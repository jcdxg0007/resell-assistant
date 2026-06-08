"""PDD 采集任务历史 —— 前端 Ops 看板 API。

读 pdd_search_runs（任务历史落库），给前端 Ops 面板出看板聚合 + 流水。

  GET  /api/v1/pdd-runs/summary   看板聚合（今日计数/成功率/趋势/最近/风控 + worker 在线）
  GET  /api/v1/pdd-runs/          分页流水（支持 status/source/keyword 过滤）

鉴权：登录用户（get_current_user）。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.system import User
from app.services.pdd_app_queue import (
    PddAppTask, await_result, clear_batch_plan, enqueue_task, get_worker_status,
    is_collection_paused, purge_queue, queue_depth, scroll_screens_for, set_batch_plan,
    set_collection_paused, set_task_meta,
)
from app.services.pdd_search_run import (
    clear_today, console_data, list_runs, paginated_items,
    persist_search_run, summary,
)
from app.services.pdd_worker_config import get_runtime_config

logger = logging.getLogger(__name__)
router = APIRouter()

# 紧急派发用：priority=9 让 worker LPUSH 插队 + 跳过 inter-burst 静默期
_DISPATCH_PRIORITY = 9
# 批量任务用普通优先级，让 worker 按 BurstScheduler 拟人节奏慢慢消化
_BATCH_PRIORITY = 1
# 后台 await 任务的强引用集合，防止 create_task 出来的协程被 GC 提前回收
_bg_tasks: set[asyncio.Task] = set()
# 批量任务的后台等待协程，暂停时统一取消（停止派发 + 不再等已清掉的任务）
_batch_tasks: set[asyncio.Task] = set()


@router.get("/summary", summary="Ops 看板聚合")
async def read_summary(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    data = await summary(db)
    data["worker"] = await get_worker_status()
    return data


class DispatchBody(BaseModel):
    """手动派发一个 PDD 搜索任务。"""

    keyword: str = Field(..., min_length=1, max_length=128)
    mode: str = Field("fast", description="fast / deep")
    harvest_dips: int | None = Field(
        None, ge=0, le=5,
        description="仅 deep 生效：进 K 个详情页收割详情。留空=用运行时配置默认值。",
    )


async def _lookup_keyword(
    db: AsyncSession, keyword_text: str
) -> tuple[str | None, str | None]:
    """按精确文本在词库里找该词，返回 (keyword_id, category_name)。

    找不到（临时手搜、不在库里）→ (None, None)，不会新建关键词。
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models.selection import Keyword

    row = (
        await db.execute(
            select(Keyword)
            .where(Keyword.text == keyword_text)
            .options(selectinload(Keyword.category))
            .limit(1)
        )
    ).scalar_one_or_none()
    if not row:
        return None, None
    cat_name = row.category.name if row.category else None
    return str(row.id), cat_name


async def _write_back_keyword(keyword_id: str | None, status: str) -> None:
    """把一次 PDD 搜索结果回写到词库（上次跑时间 / 状态 / 累计次数）。

    手动/紧急派发原先不回写词库，导致跑过的词在词库里仍显示「从未跑过」。
    这里复用 autobatch 的回写逻辑，命中词库的词（keyword_id 非空）才回写；
    临时手搜、不在库里的词（keyword_id=None）不处理、也不新建。
    失败只记日志，绝不影响主流程。
    """
    if not keyword_id:
        return
    try:
        from app.services.pdd_autobatch import _write_back_result
        await _write_back_result(keyword_id, status, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 — 回写失败不阻断采集
        logger.warning(f"pdd keyword write-back failed (kid={keyword_id}): {exc}")


async def _await_and_persist(
    task_id: str, keyword: str, mode: str, timeout_s: int,
    *, source: str = "manual", priority: int = _DISPATCH_PRIORITY,
    keyword_id: str | None = None, category_name: str | None = None,
    write_timeout: bool = True,
) -> None:
    """后台等 worker 把结果推回来，再落到 pdd_search_runs。

    跑在 FastAPI 事件循环里、不阻塞派发请求的响应。worker 离线/慢/静默都
    由 await_result 超时兜底，绝不卡住进程。

    - source/priority：手动派发 = manual/9；批量跑池 = batch/1
    - write_timeout=False：超时不落 timeout 行（批量任务被清队列后会超时，
      不该留误导性的超时记录）
    """
    try:
        from app.services.pdd_app_queue import acquire_persist_lock
        from app.services.pdd_autobatch import persist_pdd_result
        result = await await_result(task_id, timeout_s=timeout_s)
        if result is None:
            # 真超时：worker 没回结果。走幂等锁，避免和 /result 即时落库重复。
            if write_timeout and await acquire_persist_lock(task_id):
                await persist_search_run(
                    status="timeout", keyword_text=keyword, task_id=task_id,
                    source=source, mode=mode, priority=priority,
                    keyword_id=keyword_id, category_name=category_name,
                )
                await _write_back_keyword(keyword_id, "timeout")
            return
        # 有结果：交给统一落库入口（内部抢锁去重，/result 已落过则跳过）。
        await persist_pdd_result(result, {
            "keyword_id": keyword_id, "keyword_text": keyword,
            "category_name": category_name, "mode": mode,
            "source": source, "priority": priority,
        })
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — 后台任务异常只记日志
        logger.warning(f"pdd await/persist failed (kw='{keyword}'): {exc}")


@router.post("/dispatch", summary="手动派发一个 PDD 搜索任务（紧急插队）")
async def dispatch_search(
    body: DispatchBody,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """前端「PDD搜索 / 同时搜」用：紧急派一个 PDD search 任务（插队 + 跳静默）。

    立即返回 task_id（不阻塞），后台协程负责 await 结果并落库。前端稍后刷新
    «拼多多采集结果» 即可看到。worker 离线直接 503，让前端给出明确提示。

    目标商品数在运行时配置的 [target_count_min, target_count_max] 之间随机取，
    动态调整采集量。
    """
    wstatus = await get_worker_status()
    if not wstatus.get("online"):
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pdd_worker_offline",
        )
    mode = "deep" if body.mode == "deep" else "fast"
    # fast 现在按 target_count 最多滚 5 屏，单次耗时比单屏长，超时相应放宽
    task_timeout = 180 if mode == "deep" else 150
    cfg = await get_runtime_config(db)
    lo = int(cfg.get("target_count_min") or 8)
    hi = int(cfg.get("target_count_max") or 20)
    if lo > hi:
        lo, hi = hi, lo
    target_count = random.randint(lo, hi)
    dispatch_payload: dict[str, Any] = {
        "keyword": body.keyword.strip(), "mode": mode, "target_count": target_count,
        "scroll_screens": scroll_screens_for(target_count),
    }
    if mode == "deep":
        # 前端显式给了就用，否则回落运行时配置 deep_harvest_dips
        if body.harvest_dips is not None:
            dips = max(0, min(int(body.harvest_dips), 5))
        else:
            dips = max(0, min(int(cfg.get("deep_harvest_dips") or 0), 5))
        if dips > 0:
            dispatch_payload["harvest_dips"] = dips
    task = PddAppTask(
        kind="search",
        payload=dispatch_payload,
        priority=_DISPATCH_PRIORITY,
        timeout_s=task_timeout,
    )
    await enqueue_task(task)
    # 命中词库的词，紧急搜完也要回写「上次跑/累计次数」到词库（否则一直显示"从未跑过"）。
    kw_id, cat_name = await _lookup_keyword(db, body.keyword.strip())
    # 存 task-meta，供 worker /result 回传时即时落库（不依赖后台等待任务）。
    await set_task_meta(task.task_id, {
        "keyword_id": kw_id, "keyword_text": body.keyword.strip(),
        "category_name": cat_name, "mode": mode,
        "source": "manual", "priority": _DISPATCH_PRIORITY,
    })
    bg = asyncio.create_task(
        _await_and_persist(
            task.task_id, body.keyword.strip(), mode, task_timeout + 60,
            keyword_id=kw_id, category_name=cat_name,
        )
    )
    _bg_tasks.add(bg)
    bg.add_done_callback(_bg_tasks.discard)
    logger.info(f"pdd dispatch: task_id={task.task_id} keyword='{body.keyword}' mode={mode}")
    return {"ok": True, "task_id": task.task_id, "keyword": body.keyword.strip(), "mode": mode}


class BatchStartBody(BaseModel):
    """开始今日批量任务。platform 选择跑哪个平台的待采集词。"""

    platform: str = Field("both", description="pdd / xianyu / both")


def _estimate_pdd_etas(
    count: int, *, cfg: dict[str, Any], wstatus: dict[str, Any], now: float,
) -> list[float]:
    """预估批量入队的前 count 个 PDD 任务各自的开始时刻（相对 now 的秒数列表）。

    用 worker 心跳上报的 BurstScheduler 快照做前向模拟：知道当前在 burst 内还剩
    几个 / 还是 inter-burst 静默期、距上次动作多久，比"假设从零开始"准得多。
    没有快照（旧 worker / 刚上线）时退化为"立即从新 burst 开始"。
    """
    def _avg(a: float, b: float) -> float:
        return (float(a) + float(b)) / 2.0

    bs = max(1, round(_avg(cfg.get("burst_size_min", 3), cfg.get("burst_size_max", 5))))
    pace = float(cfg.get("humanize_pace", 1.0) or 1.0)
    intra = _avg(cfg.get("intra_burst_gap_seconds_min", 5),
                 cfg.get("intra_burst_gap_seconds_max", 30)) * pace
    inter = _avg(cfg.get("inter_burst_gap_minutes_min", 5),
                 cfg.get("inter_burst_gap_minutes_max", 30)) * 60.0
    per_task = 45.0  # fast 模式单次搜索 worker 端约耗时

    snap = wstatus.get("scheduler") or {}
    burst_remaining = int(snap.get("burst_remaining") or 0)
    in_quiet = bool(snap.get("in_quiet"))

    # 心跳是几秒前发的，把快照里的 *_ago 量补偿到"现在"
    age = 0.0
    ts = wstatus.get("ts")
    if ts:
        try:
            age = max(0.0, now - datetime.fromisoformat(ts).timestamp())
        except (ValueError, TypeError):
            age = 0.0
    lsa = snap.get("last_search_ago_s")
    last_search_ago = (float(lsa) + age) if lsa is not None else None
    qe = snap.get("quiet_elapsed_s")
    quiet_elapsed = (float(qe) + age) if qe is not None else None

    etas: list[float] = []
    t = 0.0
    rem = burst_remaining
    first = True
    for _ in range(count):
        if rem > 0:
            # burst 内：下一个任务等 intra-gap（从上次搜索结束算）
            if first and last_search_ago is not None:
                t += max(0.0, intra - last_search_ago)
            elif not first:
                t += per_task + intra
            rem -= 1
        else:
            # 需要开新 burst：等 inter 静默
            if first and in_quiet and quiet_elapsed is not None:
                t += max(0.0, inter - quiet_elapsed)
            elif not first:
                t += per_task + inter
            # first 且非 quiet（worker 空闲/没跑过）→ 立即开 burst，t 不变
            rem = bs - 1
        etas.append(t)
        first = False
    return etas


@router.post("/batch/start", summary="开始批量跑今日待采集池（按平台 pdd/xianyu/both）")
async def batch_start(
    body: BatchStartBody,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """把今日待采集池的词按平台批量派发：

    - platform=pdd：只把 pdd_pending 的词排进 PDD 队列（受 daily_search_quota 限制），
      worker 按 BurstScheduler 拟人节奏慢慢消化。
    - platform=xianyu：只把 xianyu_pending 的词错峰派闲鱼采集（不依赖 worker）。
    - platform=both：两边各按各自待采集集合派。
    """
    plat = body.platform if body.platform in ("pdd", "xianyu", "both") else "both"
    want_pdd = plat in ("pdd", "both")
    want_xianyu = plat in ("xianyu", "both")

    wstatus = await get_worker_status()
    if want_pdd and not wstatus.get("online"):
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail="pdd_worker_offline",
        )
    await set_collection_paused(False)
    data = await console_data(db)
    pending = data["pending"]
    cfg = await get_runtime_config(db)
    lo = int(cfg.get("target_count_min") or 8)
    hi = int(cfg.get("target_count_max") or 20)
    if lo > hi:
        lo, hi = hi, lo

    now = time.time()
    plan: dict[str, dict] = {}

    # ── PDD 侧：只取 pdd_pending 的词，受当日配额限制 ──
    pdd_enqueued = 0
    if want_pdd:
        pdd_pending = [k for k in pending if k.get("pdd_pending")]
        quota = int(cfg.get("daily_search_quota") or 30)
        remaining = max(0, quota - int(data["stats"]["total"]))
        pdd_batch = pdd_pending[:remaining]
        pdd_etas = _estimate_pdd_etas(len(pdd_batch), cfg=cfg, wstatus=wstatus, now=now)
        for idx, kw in enumerate(pdd_batch):
            tc = random.randint(lo, hi)
            task = PddAppTask(
                kind="search",
                payload={
                    "keyword": kw["text"], "mode": "fast", "target_count": tc,
                    "scroll_screens": scroll_screens_for(tc),
                },
                priority=_BATCH_PRIORITY,
                timeout_s=150,
            )
            await enqueue_task(task)
            await set_task_meta(task.task_id, {
                "keyword_id": kw["keyword_id"], "keyword_text": kw["text"],
                "category_name": kw["category_name"], "mode": "fast",
                "source": "batch", "priority": _BATCH_PRIORITY,
            })
            bg = asyncio.create_task(_await_and_persist(
                task.task_id, kw["text"], "fast", 4 * 3600,
                source="batch", priority=_BATCH_PRIORITY,
                keyword_id=kw["keyword_id"], category_name=kw["category_name"],
                write_timeout=False,
            ))
            _batch_tasks.add(bg)
            bg.add_done_callback(_batch_tasks.discard)
            plan.setdefault(kw["text"], {})["pdd"] = now + pdd_etas[idx]
        pdd_enqueued = len(pdd_batch)

    # ── 闲鱼 侧：只取 xianyu_pending 的词，错峰派发（闲鱼有自己的 ≥60s/40h 合规闸）──
    xy_scheduled = 0
    if want_xianyu:
        xy_batch = [k for k in pending if k.get("xianyu_pending")]
        if xy_batch:
            from app.tasks.selection import instant_search
            from app.models.selection import Keyword
            from sqlalchemy import update as _sa_update
            xy_offset = 0
            xy_ids: list[str] = []
            for kw in xy_batch:
                try:
                    instant_search.apply_async(args=(kw["text"], "xianyu", "batch"), countdown=xy_offset)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"batch xianyu dispatch failed (kw='{kw['text']}'): {exc}")
                    continue
                plan.setdefault(kw["text"], {})["xianyu"] = now + xy_offset
                xy_offset += random.randint(70, 110)
                xy_scheduled += 1
                if kw.get("keyword_id"):
                    xy_ids.append(kw["keyword_id"])
            if xy_ids:  # 乐观写回，防自动 tick 又挑到同词
                await db.execute(
                    _sa_update(Keyword).where(Keyword.id.in_(xy_ids))
                    .values(xianyu_last_searched_at=datetime.now(timezone.utc))
                )
                await db.commit()

    await set_batch_plan(plan)
    logger.info(
        f"pdd batch start: platform={plat} pdd_enqueued={pdd_enqueued} "
        f"xianyu_scheduled={xy_scheduled} (pending={len(pending)})"
    )
    return {
        "ok": True, "platform": plat,
        "enqueued": pdd_enqueued, "xianyu_scheduled": xy_scheduled,
        "pending_total": len(pending),
    }


@router.post("/batch/pause", summary="暂停批量任务（停止派发 + 清掉队列里还没跑的）")
async def batch_pause(
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """暂停：标记暂停（fire_from_lib 轮播会跳过）+ 清空队列里还没被 worker 拉走的，
    并取消批量后台等待协程。已被 worker 拉走、正在跑的不打断。
    """
    await set_collection_paused(True)
    purged = await purge_queue()
    await clear_batch_plan()
    for t in list(_batch_tasks):
        t.cancel()
    _batch_tasks.clear()
    logger.info(f"pdd batch pause: purged={purged}")
    return {"ok": True, "paused": True, "purged": purged}


@router.post("/batch/resume", summary="恢复采集（解除暂停，闲鱼/PDD 自动跑批可继续）")
async def batch_resume(
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """解除全局暂停标志。PDD/闲鱼 自动跑批 tick 下个唤醒周期即可继续派词。"""
    await set_collection_paused(False)
    logger.info("collection resume: paused flag cleared")
    return {"ok": True, "paused": False}


@router.get("/console", summary="今日搜索任务控制台（统计+待采集/已采集池+商品量范围+worker）")
async def read_console(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    data = await console_data(db)
    data["worker"] = await get_worker_status()
    data["paused"] = await is_collection_paused()
    data["queued"] = await queue_depth()
    from app.services.pdd_autobatch import get_routing_status
    data["routing"] = await get_routing_status(db)
    return data


@router.get("/items", summary="今日采集到的逐条商品（给词=该词，不给=全部，分页）")
async def read_items(
    keyword: str | None = Query(None, description="关键词文本；留空=今日全部"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    return await paginated_items(db, keyword, page=page, page_size=page_size)


@router.delete("/today", summary="清空今日采集记录（给 keyword 则只清该词，否则清全部）")
async def clear_today_runs(
    keyword: str | None = Query(None, description="只清这个关键词；留空清今日全部"),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    deleted = await clear_today(db, keyword)
    return {"ok": True, "deleted": deleted}


class RequeueBody(BaseModel):
    keyword: str = Field(..., min_length=1, description="要重回待采集池的关键词")


@router.post("/requeue", summary="把失败的词重回待采集池（删今日该词 PDD 记录，交给正常跑批重采）")
async def requeue_keyword(
    body: RequeueBody,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """删掉该词今天的 PDD 采集记录，使其重新落回「待采集池」。

    不做紧急派发——词回到池子后，由自动跑批按正常节奏（活跃时段+随机间隔+配额）
    重新消化，避免插队打乱节奏被识别为机器行为。
    """
    kw = body.keyword.strip()
    if not kw:
        raise HTTPException(status_code=400, detail="keyword 不能为空")
    deleted = await clear_today(db, kw)
    logger.info(f"requeue keyword='{kw}' deleted_runs={deleted}")
    return {"ok": True, "keyword": kw, "deleted": deleted}


@router.get("/", summary="任务历史流水（分页，PDD + 闲鱼合并）")
async def read_runs(
    status: str | None = Query(None, description="过滤状态：ok/empty/partial/failed/risk_blocked/timeout"),
    source: str | None = Query(None, description="过滤来源：lib/selection/batch/manual/emergency"),
    keyword: str | None = Query(None, description="关键词模糊匹配"),
    platform: str | None = Query(None, description="平台：pdd / xianyu / logistics / 留空=全部合并"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """任务记录：PDD（pdd_search_runs）+ 闲鱼（xianyu_search_runs）+ 查快递
    （logistics_runs）按时间倒序合并。

    platform=pdd / xianyu / logistics 只看单类；留空=全部合并。合并分页用"各取前
    offset+limit 条 → 归并排序 → 切片"，对前几页（页大小 20）足够精确。
    """
    from app.services.xianyu_search_run import count_xianyu_runs, list_xianyu_runs
    from app.services.logistics_run import count_logistics_runs, list_logistics_runs

    plat = (platform or "").strip().lower()

    if plat == "pdd":
        return await list_runs(
            db, status=status, source=source, keyword=keyword,
            limit=limit, offset=offset,
        )
    if plat == "xianyu":
        total = await count_xianyu_runs(db, status=status, source=source, keyword=keyword)
        rows = await list_xianyu_runs(
            db, status=status, source=source, keyword=keyword, limit=offset + limit,
        )
        return {"total": total, "items": rows[offset:offset + limit]}
    if plat == "logistics":
        total = await count_logistics_runs(db, status=status, keyword=keyword)
        rows = await list_logistics_runs(db, status=status, keyword=keyword, limit=offset + limit)
        return {"total": total, "items": rows[offset:offset + limit]}

    # 合并：各取前 offset+limit 条，归并按 created_at 倒序，再切片
    fetch_n = offset + limit
    pdd = await list_runs(db, status=status, source=source, keyword=keyword, limit=fetch_n, offset=0)
    xy_rows = await list_xianyu_runs(db, status=status, source=source, keyword=keyword, limit=fetch_n)
    xy_total = await count_xianyu_runs(db, status=status, source=source, keyword=keyword)
    merged = list(pdd["items"]) + list(xy_rows)
    total = int(pdd["total"]) + int(xy_total)
    # 查快递无"采集来源(source)"语义，仅当未按 source 过滤时并入合并视图
    if not source:
        lg_rows = await list_logistics_runs(db, status=status, keyword=keyword, limit=fetch_n)
        lg_total = await count_logistics_runs(db, status=status, keyword=keyword)
        merged += list(lg_rows)
        total += int(lg_total)
    merged.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {
        "total": total,
        "items": merged[offset:offset + limit],
    }
