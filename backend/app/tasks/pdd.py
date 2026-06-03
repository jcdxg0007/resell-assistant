"""PDD 全自动跑批 celery 任务。

设计为「自带闸门的高频 tick」：beat 每 3 分钟唤醒一次 auto_batch_tick，
但是否真派由任务内部判断（开关 / 暂停标志 / 活跃时段 / 随机下次时刻 / 配额 /
worker 在线）。这样跑批时刻每天随机错峰，不会像固定 crontab 那样形成
「每天 X 点准时上线」的机器指纹；且频率/时段/词数全在前端配置里可调，
不用改 crontab 重新部署。

派发与落库分离：tick 只 enqueue（立即返回），结果由每词一个
auto_await_persist 任务并发等待 + 落库，避免单个 tick 长时间占住 worker。
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

from loguru import logger

from app.core.celery_app import celery_app
from app.core.database import AsyncSessionLocal
from app.services.pdd_app_queue import (
    get_auto_next_ts, get_worker_status, is_collection_paused, set_auto_next_ts,
)
from app.services.pdd_autobatch import (
    await_and_persist_one, dispatch_auto_batch, load_account_assignments,
)
from app.services.pdd_worker_config import get_runtime_config
from app.tasks.selection import run_async

_CN_TZ = timezone(timedelta(hours=8))


def _in_window(hour: int, start: int, end: int) -> bool:
    """当前小时是否在活跃时段内。start==end 视为全天；start>end 视为跨夜。"""
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


@celery_app.task(name="app.tasks.pdd.auto_batch_tick")
def auto_batch_tick():
    run_async(_auto_batch_tick())


async def _auto_batch_tick():
    async with AsyncSessionLocal() as db:
        cfg = await get_runtime_config(db)
        assignments = await load_account_assignments(db)

    if not cfg.get("auto_batch_enabled"):
        return
    if await is_collection_paused():
        logger.info("auto_batch tick: 采集已暂停，跳过")
        return

    hour = datetime.now(_CN_TZ).hour
    start = int(cfg.get("auto_active_start_hour", 9))
    end = int(cfg.get("auto_active_end_hour", 23))
    if not _in_window(hour, start, end):
        return  # 不在活跃时段，不动 next_ts（时段一开就能立刻跑）

    status = await get_worker_status()
    if not status.get("online"):
        logger.info("auto_batch tick: worker 离线，跳过本轮")
        return

    count = int(cfg.get("auto_batch_count", 3))
    gmin = int(cfg.get("auto_interval_min_minutes", 40))
    gmax = int(cfg.get("auto_interval_max_minutes", 120))
    if gmin > gmax:
        gmin, gmax = gmax, gmin

    # ── 多号路由模式（roadmap §15）：配过「品类↔号」绑定就按号独立派发 ──
    if assignments:
        online_accounts = set(status.get("accounts") or [])
        if not online_accounts:
            logger.info("auto_batch tick: 已配多号绑定但无在线号上报 account，跳过")
            return
        for account_id, info in assignments.items():
            acct_name = info["account_name"]
            if acct_name not in online_accounts:
                continue  # 该号不在线 → 它的品类等它上线再跑
            now = time.time()
            next_ts = await get_auto_next_ts(account=acct_name)
            if next_ts is not None and now < next_ts:
                continue  # 该号还没到自己的随机时刻（各号独立错峰）
            await set_auto_next_ts(now + random.uniform(gmin, gmax) * 60, account=acct_name)
            # 闲鱼有独立自动 tick，多号模式下 PDD 派发不带闲鱼，避免按号重复触发
            descs = await dispatch_auto_batch(
                count=count, both_platforms=False,
                account_name=acct_name, account_id=account_id,
                allowed_category_ids=info["category_ids"],
            )
            for d in descs:
                auto_await_persist.apply_async(args=(d,))
        return

    # ── 旧的全局模式（还没配任何绑定）：派到默认队列，不因漏配停采 ──
    now = time.time()
    next_ts = await get_auto_next_ts()
    if next_ts is not None and now < next_ts:
        return  # 还没到下次随机时刻
    await set_auto_next_ts(now + random.uniform(gmin, gmax) * 60)
    both = bool(cfg.get("auto_both_platforms", True))
    descs = await dispatch_auto_batch(count=count, both_platforms=both)
    for d in descs:
        auto_await_persist.apply_async(args=(d,))


@celery_app.task(name="app.tasks.pdd.auto_await_persist")
def auto_await_persist(desc: dict):
    """等一个自动派词的结果并落库（每词一个，并发跑，可阻塞至单任务超时）。"""
    run_async(await_and_persist_one(desc, source="auto"))
