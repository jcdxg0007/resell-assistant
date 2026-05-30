"""闲鱼全自动采集核心逻辑（供 celery beat tick + 「开始任务」批量共用）。

与 PDD 自动跑批（pdd_autobatch.py）对称，但更简单：闲鱼不走家里手机 worker，
直接用 celery 的 instant_search 任务采集；速率由闲鱼自身的合规闸控制
（≥60s 间隔 + 40/h 上限），这里只负责「从词库挑词 + 错峰派发」。

选词：词库里 xianyu_safe & is_active & schedule_enabled 的词，按
xianyu_last_searched_at 最久没跑的优先（NULLS FIRST），取 N 个。
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.models.selection import Keyword

logger = logging.getLogger(__name__)


async def select_xianyu_keywords(db, count: int) -> list[Keyword]:
    """挑 N 个闲鱼可调度词：xianyu_safe + 在用 + 启用，最久没跑闲鱼的优先。"""
    stmt = (
        select(Keyword)
        .options(selectinload(Keyword.category))
        .where(Keyword.xianyu_safe.is_(True))
        .where(Keyword.is_active.is_(True))
        .where(Keyword.schedule_enabled.is_(True))
        .order_by(
            Keyword.xianyu_last_searched_at.asc().nullsfirst(),
            Keyword.xianyu_searches_total.asc(),
            func.random(),
        )
        .limit(count)
    )
    return list((await db.execute(stmt)).scalars().all())


async def dispatch_xianyu_batch(
    *, count: int, keywords: list[str] | None = None,
) -> list[str]:
    """派发闲鱼采集：挑词（或用传入的指定词）→ 错峰 apply_async → 乐观写回时间。

    :param keywords: 指定词列表（「开始任务」批量用）；None 则从词库自动挑 count 个。
    :return: 实际派发的关键词文本列表。空 = 没词可派。
    """
    from app.tasks.selection import instant_search  # 延迟导入避免循环

    now = datetime.now(timezone.utc)
    dispatched: list[str] = []

    async with AsyncSessionLocal() as db:
        if keywords is None:
            kw_rows = await select_xianyu_keywords(db, count)
        else:
            kw_rows = list((await db.execute(
                select(Keyword)
                .options(selectinload(Keyword.category))
                .where(Keyword.text.in_(keywords))
            )).scalars().all())

        if not kw_rows:
            logger.info("xianyu auto/batch: 无可派闲鱼词，跳过")
            return []

        offset = 0
        for k in kw_rows:
            try:
                instant_search.apply_async(args=(k.text, "xianyu"), countdown=offset)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"xianyu dispatch failed (kw='{k.text}'): {exc}")
                continue
            # 乐观写回，防止下一 tick 又挑到同词
            k.xianyu_last_searched_at = now
            k.xianyu_searches_total = (k.xianyu_searches_total or 0) + 1
            dispatched.append(k.text)
            offset += random.randint(70, 110)  # 错峰，顺着闲鱼 ≥60s 合规节奏
        await db.commit()

    logger.info(f"xianyu auto/batch: 派 {len(dispatched)} 个闲鱼词 {dispatched}")
    return dispatched
