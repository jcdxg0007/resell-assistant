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
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.models.pdd_run import PddSearchRun
from app.models.selection import Category, Keyword
from app.services.pdd_app_queue import (
    PddAppTask, await_result, enqueue_task, get_worker_status,
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


def price_stats(items: list[dict]) -> tuple[float | None, float | None]:
    prices = sorted(float(it["price"]) for it in items if it.get("price"))
    if not prices:
        return None, None
    return prices[0], prices[len(prices) // 2]


async def select_cohesive_keywords(
    db, count: int, category_slug: str | None = None
) -> list[Keyword]:
    """挑 N 个词，遵循「burst 内同品类聚集 + burst 间品类轮换」。

    1. 锁定品类：所有「有可调度 PDD 词」的品类里挑整体最久没碰过的那个
       （MAX(pdd_last_searched_at) ASC NULLS FIRST，random 给同级打散）。
       指定 category_slug 时跳过这步。
    2. 品类内按 pdd_last_searched_at ASC NULLS FIRST 取 N 个。

    可跑词不足 N 个时只返回那几个（不跨品类硬凑，保持 session 主题纯净）。
    """
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
        .order_by(
            Keyword.pdd_last_searched_at.asc().nullsfirst(),
            Keyword.pdd_searches_total.asc(),
            func.random(),
        )
        .limit(count)
    )
    return list((await db.execute(kw_stmt)).scalars().all())


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


async def dispatch_auto_batch(
    *, count: int, both_platforms: bool, priority: int = 1,
    category_slug: str | None = None,
) -> list[dict]:
    """自动跑批的「派发」阶段：挑词 → 入队（普通优先级）→ 乐观写回 → 闲鱼错峰。

    不等结果（结果由 celery 每词一个 await-persist 任务异步落库），保证 beat
    tick 立即返回、不长时间占住 worker。受 daily_search_quota 限制。

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

        keywords = await select_cohesive_keywords(db, count, category_slug)
        if not keywords:
            logger.info("auto_batch: 词库无可调度词，跳过")
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
            task = PddAppTask(
                kind="search",
                payload={
                    "keyword": k.text,
                    "target_count": random.randint(tc_lo, tc_hi),
                    "scroll_screens": scroll,
                    "mode": worker_mode,
                },
                priority=priority,
                timeout_s=per_task_timeout,
            )
            await enqueue_task(task)
            await _mark_dispatched(str(k.id), now0)  # 乐观写回，防重复挑中
            descs.append({
                "task_id": task.task_id,
                "keyword_id": str(k.id),
                "keyword_text": k.text,
                "worker_mode": worker_mode,
                "category_name": k.category.name if k.category else None,
                "priority": priority,
                "timeout_s": per_task_timeout,
            })

        # 闲鱼错峰派发（闲鱼有自己的合规闸：≥60s 间隔 + 40/h 上限）
        if both_platforms:
            try:
                from app.tasks.selection import instant_search
                xy_offset = 0
                for d in descs:
                    instant_search.apply_async(args=(d["keyword_text"], "xianyu"), countdown=xy_offset)
                    xy_offset += random.randint(70, 110)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"auto_batch: 闲鱼派发失败: {exc}")

    logger.info(
        f"auto_batch: 锁定品类【{cat_label}】派 {len(descs)} 词 "
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
        await _write_back_result(kid, "timeout", now)
        await persist_search_run(
            status="timeout", keyword_text=text_, keyword_id=kid,
            source=source, category_name=cat_name, mode=worker_mode,
            priority=priority,
        )
        return "timeout"

    bucket = result.status
    if bucket == "ok" and len(result.items) == 0:
        bucket = "empty"
    await _write_back_result(kid, bucket, now)
    p_min, p_median = price_stats(result.items)
    await persist_search_run(
        status=bucket, keyword_text=text_, keyword_id=kid,
        task_id=result.task_id, source=source, category_name=cat_name,
        mode=worker_mode, items_count=len(result.items),
        price_min=p_min, price_median=p_median,
        risk_signals=result.risk_signals, items=result.items,
        device_serial=result.device_serial,
        account_name=result.account_name, elapsed_ms=result.elapsed_ms,
        priority=priority, error=result.error,
    )
    return bucket
