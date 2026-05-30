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
    is_collection_paused, purge_queue, queue_depth, set_batch_plan,
    set_collection_paused,
)
from app.services.pdd_search_run import (
    clear_today, console_data, keyword_items, list_runs, persist_search_run, summary,
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
        result = await await_result(task_id, timeout_s=timeout_s)
        if result is None:
            if write_timeout:
                await persist_search_run(
                    status="timeout", keyword_text=keyword, task_id=task_id,
                    source=source, mode=mode, priority=priority,
                    keyword_id=keyword_id, category_name=category_name,
                )
            return
        items = result.items or []
        prices = sorted(float(it["price"]) for it in items if it.get("price"))
        p_min = prices[0] if prices else None
        p_median = prices[len(prices) // 2] if prices else None
        bucket = result.status
        if bucket == "ok" and not items:
            bucket = "empty"
        await persist_search_run(
            status=bucket, keyword_text=keyword, task_id=task_id, source=source,
            mode=mode, items_count=len(items), price_min=p_min, price_median=p_median,
            risk_signals=result.risk_signals, items=items, device_serial=result.device_serial,
            account_name=result.account_name, elapsed_ms=result.elapsed_ms,
            keyword_id=keyword_id, category_name=category_name,
            priority=priority, error=result.error,
        )
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
    task_timeout = 180 if mode == "deep" else 90
    cfg = await get_runtime_config(db)
    lo = int(cfg.get("target_count_min") or 8)
    hi = int(cfg.get("target_count_max") or 20)
    if lo > hi:
        lo, hi = hi, lo
    target_count = random.randint(lo, hi)
    task = PddAppTask(
        kind="search",
        payload={"keyword": body.keyword.strip(), "mode": mode, "target_count": target_count},
        priority=_DISPATCH_PRIORITY,
        timeout_s=task_timeout,
    )
    await enqueue_task(task)
    bg = asyncio.create_task(
        _await_and_persist(task.task_id, body.keyword.strip(), mode, task_timeout + 60)
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
            task = PddAppTask(
                kind="search",
                payload={"keyword": kw["text"], "mode": "fast", "target_count": random.randint(lo, hi)},
                priority=_BATCH_PRIORITY,
                timeout_s=90,
            )
            await enqueue_task(task)
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
                    instant_search.apply_async(args=(kw["text"], "xianyu"), countdown=xy_offset)
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
    return data


@router.get("/items", summary="某关键词今日采集到的逐条商品")
async def read_items(
    keyword: str = Query(..., min_length=1, description="关键词文本"),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    return await keyword_items(db, keyword)


@router.delete("/today", summary="清空今日采集记录（给 keyword 则只清该词，否则清全部）")
async def clear_today_runs(
    keyword: str | None = Query(None, description="只清这个关键词；留空清今日全部"),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    deleted = await clear_today(db, keyword)
    return {"ok": True, "deleted": deleted}


@router.get("/", summary="任务历史流水（分页）")
async def read_runs(
    status: str | None = Query(None, description="过滤状态：ok/empty/partial/failed/risk_blocked/timeout"),
    source: str | None = Query(None, description="过滤来源：lib/selection/manual/emergency"),
    keyword: str | None = Query(None, description="关键词模糊匹配"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    return await list_runs(
        db, status=status, source=source, keyword=keyword,
        limit=limit, offset=offset,
    )
