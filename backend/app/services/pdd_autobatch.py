"""PDD 全自动跑批核心逻辑（供 celery beat tick + 命令行脚本共用）。

把「从词库按品类聚集挑词 → 入队 → 等结果 → 写回 + 落库」抽成可复用的
async 函数，避免 beat 任务和 pdd_fire_from_lib.py 各写一份选词 SQL 漂移。

选词策略：burst 内同品类聚集 + burst 间品类轮换（拟人化，详见
docs/PDD-自建采集-roadmap.md §"Day 4 词库选词策略"）。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.models.pdd_run import PddSearchRun
from app.models.selection import Category, Keyword, PddCategoryAccount
from app.models.system import Account
from app.services.pdd_app_queue import (
    PddAppResult, PddAppTask, acquire_persist_lock, await_result, enqueue_task,
    get_worker_status, scroll_screens_for, set_task_meta,
)
from app.services.pdd_search_run import _cn_day_start, persist_search_run
from app.services.pdd_worker_config import get_runtime_config

logger = logging.getLogger(__name__)

# pdd_mode → (worker mode, default target_count, scroll_screens)
MODE_MAP = {
    "fast":         ("fast", 8,  2),
    "list_deep":    ("deep", 30, 5),
    "detail_smart": ("fast", 8,  2),  # Phase 2 占位
    "detail_deep":  ("fast", 8,  2),  # Phase 2 占位
}

# target_platforms 是 JSON 列，必须 ::jsonb 才能用 @>。表名写死防 join 歧义。
PDD_PLATFORM_FILTER = text(
    "selection_keywords.target_platforms::jsonb @> '[\"pdd\"]'::jsonb"
)

# 自动跑批「同词每日防重复」口径：成功(ok/partial)即当天不再重采；
# 失败/空可重试，但当日总尝试达 DAILY_MAX_ATTEMPTS 后停止（防无限重挑）。
_SUCCESS_STATUSES = ("ok", "partial")
DAILY_MAX_ATTEMPTS = 2


def price_stats(items: list[dict]) -> tuple[float | None, float | None]:
    prices = sorted(float(it["price"]) for it in items if it.get("price"))
    if not prices:
        return None, None
    return prices[0], prices[len(prices) // 2]


async def select_cohesive_keywords(
    db, count: int, category_slug: str | None = None,
    allowed_category_ids: list[str] | None = None,
) -> list[Keyword]:
    """挑 N 个词，遵循「burst 内同品类聚集 + burst 间品类轮换」。

    1. 锁定品类：所有「有可调度 PDD 词」的品类里挑整体最久没碰过的那个
       （MAX(pdd_last_searched_at) ASC NULLS FIRST，random 给同级打散）。
       指定 category_slug 时跳过这步。
    2. 品类内按 pdd_last_searched_at ASC NULLS FIRST 取 N 个。

    allowed_category_ids：多号路由时只在该号被分配的品类里挑（roadmap §15）。
    传空列表 → 该号没分配任何品类 → 返回 []（未分配=不跑）。

    可跑词不足 N 个时只返回那几个（不跨品类硬凑，保持 session 主题纯净）。

    防重复（按当日跑批记录判定，非「派过就跳」）：排除「今日（东八）已成功」
    的词（status ok/partial）——成功了当天不再重采；失败/空的词当天可再自动
    跑，但当日总尝试 ≥ DAILY_MAX_ATTEMPTS 次后也不再挑（防一直失败被无限重挑）。
    手动「开始任务」/重回队列走别的入口，不受此限。
    """
    if allowed_category_ids is not None and len(allowed_category_ids) == 0:
        return []  # 该号未分配任何品类 → 不跑

    day_start = _cn_day_start()
    today_done = (
        select(PddSearchRun.keyword_text)
        .where(PddSearchRun.created_at >= day_start)
        .where(PddSearchRun.keyword_text.isnot(None))
        .group_by(PddSearchRun.keyword_text)
        .having(or_(
            func.count().filter(PddSearchRun.status.in_(_SUCCESS_STATUSES)) > 0,
            func.count() >= DAILY_MAX_ATTEMPTS,
        ))
    )
    not_today = Keyword.text.notin_(today_done)
    if category_slug:
        cat = (await db.execute(
            select(Category).where(Category.slug == category_slug)
        )).scalar_one_or_none()
        if cat is None:
            return []
        chosen_cat_id = cat.id
    else:
        cat_stmt = (
            select(Category.id)
            .join(Keyword, Keyword.category_id == Category.id)
            .where(Keyword.pdd_safe.is_(True))
            .where(Keyword.is_active.is_(True))
            .where(Keyword.schedule_enabled.is_(True))
            .where(PDD_PLATFORM_FILTER)
            .where(not_today)
        )
        if allowed_category_ids is not None:
            cat_stmt = cat_stmt.where(Category.id.in_(allowed_category_ids))
        cat_stmt = (
            cat_stmt
            .group_by(Category.id)
            .order_by(
                func.max(Keyword.pdd_last_searched_at).asc().nullsfirst(),
                func.random(),
            )
            .limit(1)
        )
        chosen_cat_id = (await db.execute(cat_stmt)).scalar_one_or_none()
        if chosen_cat_id is None:
            return []

    kw_stmt = (
        select(Keyword)
        .options(selectinload(Keyword.category))
        .where(Keyword.category_id == chosen_cat_id)
        .where(Keyword.pdd_safe.is_(True))
        .where(Keyword.is_active.is_(True))
        .where(Keyword.schedule_enabled.is_(True))
        .where(PDD_PLATFORM_FILTER)
        .where(not_today)
        .order_by(
            Keyword.pdd_last_searched_at.asc().nullsfirst(),
            Keyword.pdd_searches_total.asc(),
            func.random(),
        )
        .limit(count)
    )
    return list((await db.execute(kw_stmt)).scalars().all())


async def load_account_assignments(db) -> dict[str, dict]:
    """读「品类↔采集号」绑定，按号聚合（roadmap §15）。

    :return: {account_id: {"account_name": str, "category_ids": [str, ...]}}
             只含 platform='pdd_crawler' 的号。空 dict = 全库还没配过任何绑定
             → dispatch 退化到旧的全局派发（不因漏配停采）。
    """
    stmt = (
        select(
            PddCategoryAccount.account_id,
            PddCategoryAccount.category_id,
            Account.account_name,
        )
        .join(Account, Account.id == PddCategoryAccount.account_id)
        .where(Account.platform == "pdd_crawler")
    )
    rows = (await db.execute(stmt)).all()
    out: dict[str, dict] = {}
    for account_id, category_id, account_name in rows:
        entry = out.setdefault(
            str(account_id), {"account_name": account_name, "category_ids": []}
        )
        entry["category_ids"].append(str(category_id))
    return out


async def get_routing_status(db) -> dict:
    """多号路由状态面板（roadmap §15，控制台 Phase 3）。

    给每个 pdd_crawler 号一行：在线？队列里堆了几个？被分配几个品类？下次自己
    随机派词的时刻？用来肉眼确认双号确实在【各采各的品类 + 错峰】跑。

    :return: {"enabled": bool, "accounts": [ {account_name, is_active,
              bound_device_serial, online, queue_depth, assigned_category_count,
              next_auto_at}, ... ]}
              enabled=False 表示全库还没配任何绑定（走旧全局派发）。
    """
    from datetime import datetime as _dt
    from app.services.pdd_app_queue import (
        account_queue_depth, get_auto_next_ts, get_worker_status,
    )

    _CN_TZ = timezone(timedelta(hours=8))

    accounts = (await db.execute(
        select(Account)
        .where(Account.platform == "pdd_crawler")
        .order_by(Account.account_name)
    )).scalars().all()

    # 每个号被分配的品类数
    assign_counts: dict[str, int] = {}
    rows = (await db.execute(
        select(PddCategoryAccount.account_id, func.count(PddCategoryAccount.category_id))
        .group_by(PddCategoryAccount.account_id)
    )).all()
    for acct_id, cnt in rows:
        assign_counts[str(acct_id)] = int(cnt)

    online = set((await get_worker_status()).get("accounts") or [])

    out: list[dict] = []
    for a in accounts:
        ts = await get_auto_next_ts(account=a.account_name)
        out.append({
            "account_name": a.account_name,
            "is_active": a.is_active,
            "bound_device_serial": a.bound_device_serial,
            "online": a.account_name in online,
            "queue_depth": await account_queue_depth(a.account_name),
            "assigned_category_count": assign_counts.get(str(a.id), 0),
            "next_auto_at": (
                _dt.fromtimestamp(ts, tz=_CN_TZ).isoformat() if ts else None
            ),
        })

    return {"enabled": bool(assign_counts), "accounts": out}


async def _today_run_count(db) -> int:
    day_start = _cn_day_start()
    return int((await db.execute(
        select(func.count()).select_from(PddSearchRun)
        .where(PddSearchRun.created_at >= day_start)
    )).scalar_one() or 0)


async def _mark_dispatched(keyword_id: str, when: datetime) -> None:
    """入队时乐观写回 pdd_last_searched_at，防止 await 期间同词被重复挑中。"""
    async with AsyncSessionLocal() as db:
        kw = await db.get(Keyword, keyword_id)
        if kw:
            kw.pdd_last_searched_at = when
            await db.commit()


async def _write_back_result(keyword_id: str, status: str, when: datetime) -> None:
    async with AsyncSessionLocal() as db:
        kw = await db.get(Keyword, keyword_id)
        if not kw:
            return
        kw.pdd_last_searched_at = when
        kw.pdd_last_status = status
        kw.pdd_searches_total = (kw.pdd_searches_total or 0) + 1
        await db.commit()


async def persist_pdd_result(result: PddAppResult, meta: dict) -> bool:
    """统一落库入口：把一个 worker 结果写回 keyword 状态 + 落 pdd_search_runs。

    幂等：先抢 acquire_persist_lock(task_id)，抢不到说明已被另一条路径（/result
    即时落库 或 await-persist 兜底）落过了，直接跳过。这样无论结果是 worker
    回传时即时落、还是等待任务兜底落，同一个 task 只会写一行、keyword 只 +1 次。

    meta 字段：keyword_id / keyword_text / category_name / mode / source / priority。
    返回 True=本次真正落了库；False=被去重跳过。
    """
    if not await acquire_persist_lock(result.task_id):
        return False
    items = result.items or []
    bucket = result.status
    if bucket == "ok" and len(items) == 0:
        bucket = "empty"
    kid = meta.get("keyword_id")
    if kid:
        await _write_back_result(kid, bucket, datetime.now(timezone.utc))
    p_min, p_median = price_stats(items)
    await persist_search_run(
        status=bucket, keyword_text=meta.get("keyword_text") or "",
        keyword_id=kid, task_id=result.task_id, source=meta.get("source", "auto"),
        category_name=meta.get("category_name"), mode=meta.get("mode"),
        items_count=len(items), price_min=p_min, price_median=p_median,
        risk_signals=result.risk_signals, items=items,
        device_serial=result.device_serial, account_name=result.account_name,
        elapsed_ms=result.elapsed_ms, priority=meta.get("priority", 1),
        error=result.error,
    )
    return True


async def dispatch_auto_batch(
    *, count: int, both_platforms: bool, priority: int = 1,
    category_slug: str | None = None,
    account_name: str | None = None,
    account_id: str | None = None,
    allowed_category_ids: list[str] | None = None,
) -> list[dict]:
    """自动跑批的「派发」阶段：挑词 → 入队（普通优先级）→ 乐观写回 → 闲鱼错峰。

    不等结果（结果由 celery 每词一个 await-persist 任务异步落库），保证 beat
    tick 立即返回、不长时间占住 worker。受 daily_search_quota 限制。

    多号路由（roadmap §15）：给定 account_name + allowed_category_ids 时，只在
    该号被分配的品类里挑词，并入该号专属队列；为空 → 旧的全局派发到默认队列。

    :return: 每个已派词的描述 dict（task_id/keyword_id/.../timeout_s），供
             await-persist 任务消费。空 list = 没派（配额满 / 无可调度词）。
    """
    async with AsyncSessionLocal() as db:
        cfg = await get_runtime_config(db)
        quota = int(cfg.get("daily_search_quota") or 30)
        today = await _today_run_count(db)
        remaining = max(0, quota - today)
        if remaining <= 0:
            logger.info(f"auto_batch: 今日已达配额 {today}/{quota}，跳过")
            return []
        count = min(count, remaining)

        keywords = await select_cohesive_keywords(
            db, count, category_slug, allowed_category_ids=allowed_category_ids
        )
        if not keywords:
            who = f"号【{account_name}】" if account_name else ""
            logger.info(f"auto_batch: {who}无可调度词，跳过")
            return []

        tc_lo = int(cfg.get("target_count_min") or 8)
        tc_hi = int(cfg.get("target_count_max") or 20)
        if tc_lo > tc_hi:
            tc_lo, tc_hi = tc_hi, tc_lo
        # 单任务超时给足：worker 最坏要先等满一个 inter-burst 静默才开新 burst
        per_task_timeout = int(float(cfg.get("inter_burst_gap_minutes_max", 30)) * 60) + 600

        cat_label = keywords[0].category.name if keywords[0].category else "?"
        descs: list[dict] = []
        now0 = datetime.now(timezone.utc)
        for k in keywords:
            worker_mode, _d, scroll = MODE_MAP.get(k.pdd_mode, MODE_MAP["fast"])
            tc = random.randint(tc_lo, tc_hi)
            # fast 模式按 target_count 推导滚屏数（够数即停），深抓模式沿用 MODE_MAP
            eff_scroll = scroll_screens_for(tc) if worker_mode == "fast" else scroll
            task = PddAppTask(
                kind="search",
                payload={
                    "keyword": k.text,
                    "target_count": tc,
                    "scroll_screens": eff_scroll,
                    "mode": worker_mode,
                },
                account_id=account_id,
                priority=priority,
                timeout_s=per_task_timeout,
            )
            await enqueue_task(task, account=account_name)
            await _mark_dispatched(str(k.id), now0)  # 乐观写回，防重复挑中
            desc = {
                "task_id": task.task_id,
                "keyword_id": str(k.id),
                "keyword_text": k.text,
                "worker_mode": worker_mode,
                "category_name": k.category.name if k.category else None,
                "priority": priority,
                "timeout_s": per_task_timeout,
            }
            # 存一份 task-meta，供 worker /result 回传时即时落库（不依赖等待任务）
            await set_task_meta(task.task_id, {
                "keyword_id": str(k.id),
                "keyword_text": k.text,
                "category_name": desc["category_name"],
                "mode": worker_mode,
                "source": "auto",
                "priority": priority,
                "account_name": account_name,
            })
            descs.append(desc)

        # 闲鱼错峰派发（闲鱼有自己的合规闸：≥60s 间隔 + 40/h 上限）
        if both_platforms:
            try:
                from app.tasks.selection import instant_search
                xy_offset = 0
                for d in descs:
                    instant_search.apply_async(args=(d["keyword_text"], "xianyu", "lib"), countdown=xy_offset)
                    xy_offset += random.randint(70, 110)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"auto_batch: 闲鱼派发失败: {exc}")

    who = f"号【{account_name}】" if account_name else ""
    logger.info(
        f"auto_batch: {who}锁定品类【{cat_label}】派 {len(descs)} 词 "
        f"both={both_platforms} (今日 {today}/{quota})"
    )
    return descs


async def await_and_persist_one(desc: dict, source: str = "auto") -> str:
    """等一个已派词的结果 → 写回 keyword 状态 + 落库 pdd_search_runs。

    供 celery 每词一个任务并发调用，避免单个 tick 阻塞数十分钟。
    """
    result = await await_result(desc["task_id"], timeout_s=desc["timeout_s"])
    now = datetime.now(timezone.utc)
    kid = desc["keyword_id"]
    text_ = desc["keyword_text"]
    worker_mode = desc["worker_mode"]
    cat_name = desc.get("category_name")
    priority = desc.get("priority", 1)

    if result is None:
        # 真超时：worker 没回结果。也走幂等锁——万一结果其实在 /result 落过了
        # （比如等待任务自己延迟），就别再写一行误导性的 timeout。
        if await acquire_persist_lock(desc["task_id"]):
            await _write_back_result(kid, "timeout", now)
            await persist_search_run(
                status="timeout", keyword_text=text_, keyword_id=kid,
                source=source, category_name=cat_name, mode=worker_mode,
                priority=priority,
            )
        return "timeout"

    # 有结果：交给统一落库入口（内部抢锁去重，/result 已落过则跳过）。
    meta = {
        "keyword_id": kid, "keyword_text": text_, "category_name": cat_name,
        "mode": worker_mode, "source": source, "priority": priority,
    }
    await persist_pdd_result(result, meta)
    bucket = result.status
    if bucket == "ok" and len(result.items) == 0:
        bucket = "empty"
    return bucket
