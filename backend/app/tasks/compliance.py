"""
Celery tasks enforcing the four hard compliance rules.

Currently:
- ``enforce_product_cap``: keeps the ``products`` table ≤ 100 000 rows by
  deleting the oldest crawl-only entries (FIFO by ``last_crawled_at``).
  Protected rows — anything referenced by an active listing or order —
  are never deleted, so the cap may transiently exceed the configured
  limit when too many rows are "in use". That's acceptable: the rule
  targets crawler pollution, not business data.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import text

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.tasks.selection import run_async


@celery_app.task(name="app.tasks.compliance.enforce_product_cap")
def enforce_product_cap():
    """Daily FIFO rotation of the products table.

    Target: keep ``COUNT(products)`` ≤ ``settings.PRODUCT_LIBRARY_CAP``.
    Eviction order: ``last_crawled_at NULLS FIRST, created_at ASC``
    (stalest data goes first). Business-linked rows are excluded.
    """
    logger.info("compliance: starting enforce_product_cap")
    run_async(_enforce_product_cap())


async def _enforce_product_cap():
    cap = get_settings().PRODUCT_LIBRARY_CAP
    async with AsyncSessionLocal() as db:
        # Fast COUNT(*) — postgres + index on products doesn't make this
        # expensive at 100k scale (<20ms). If the table grows into
        # multi-million range we'd swap this for pg_class.reltuples.
        total = (await db.execute(text("SELECT count(*) FROM products"))).scalar_one()
        logger.info(f"compliance: products table has {total} rows (cap={cap})")

        if total <= cap:
            logger.info("compliance: under cap, nothing to evict")
            return {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "total": total, "cap": cap, "deleted": 0,
            }

        to_delete = total - cap
        logger.warning(
            f"compliance: exceeding cap by {to_delete} rows, evicting FIFO"
        )

        # Delete the oldest crawl-only rows in one statement. The
        # NOT EXISTS clauses protect any product that's:
        #   - currently listed on xianyu (xianyu_listings.product_id)
        #   - referenced by an order (orders.product_id is SET NULL on
        #     cascade, but for safety we also skip rows currently in
        #     the orders table)
        #   - linked as source/target in a product_match pair
        # This keeps business data intact no matter how long the FIFO
        # sweep runs.
        #
        # ``ctid`` is postgres's physical row identifier — using it
        # here instead of ``id`` avoids building a UUID IN-list of
        # 10 000 elements on each run.
        result = await db.execute(
            text(
                """
                WITH victims AS (
                    SELECT p.id
                    FROM products p
                    WHERE NOT EXISTS (
                        SELECT 1 FROM xianyu_listings xl
                        WHERE xl.product_id = p.id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM orders o
                        WHERE o.product_id = p.id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM product_matches pm
                        WHERE pm.source_product_id = p.id
                           OR pm.target_product_id = p.id
                    )
                    ORDER BY p.last_crawled_at ASC NULLS FIRST,
                             p.created_at ASC
                    LIMIT :n
                )
                DELETE FROM products WHERE id IN (SELECT id FROM victims)
                """
            ),
            {"n": to_delete},
        )
        await db.commit()
        deleted = result.rowcount or 0

        logger.warning(
            f"compliance: enforce_product_cap deleted {deleted} stale "
            f"product rows (target was {to_delete})"
        )
        if deleted < to_delete:
            logger.info(
                f"compliance: could only evict {deleted}/{to_delete} — "
                f"remaining excess is business-linked and will be kept"
            )
        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "total_before": total,
            "cap": cap,
            "deleted": deleted,
            "unevictable": max(0, to_delete - deleted),
        }


@celery_app.task(name="app.tasks.compliance.daily_purge_collected")
def daily_purge_collected():
    """每日清库：把超过「保留窗口」的闲鱼采集商品物理删掉。

    与「选品池/任务流水」口径统一：保留天数走运行时配置 xianyu_runs_retention_days
    （前端「数据清理」可改），默认回落 XIANYU_RUNS_RETENTION_DAYS。这样十维度选品
    页的闲鱼侧能往回看 N 天，而不是每天 3 点归零。

    保留：① 人工 Pin 的（pinned_at 非空，收藏永不清）；② 保留窗口内又采到的
    （last_crawled_at >= now - N 天）；③ 业务关联行（在卖挂牌/订单/匹配对）。
    只清 source_platform=XIANYU 的采集结果——PDD 采集结果存在 pdd_search_runs 流水里。
    """
    logger.info("compliance: starting daily_purge_collected")
    return run_async(_daily_purge_collected())


async def _daily_purge_collected():
    async with AsyncSessionLocal() as db:
        # 闲鱼商品保留天数与「闲鱼流水保留」统一，前端可改，读不到回落常量。
        try:
            from app.services.pdd_worker_config import get_runtime_config
            cfg = await get_runtime_config(db)
            retention = int(cfg.get("xianyu_runs_retention_days") or XIANYU_RUNS_RETENTION_DAYS)
        except Exception:
            retention = XIANYU_RUNS_RETENTION_DAYS
        retention = max(1, retention)
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
        result = await db.execute(
            text(
                """
                WITH victims AS (
                    SELECT p.id
                    FROM products p
                    WHERE p.source_platform = 'XIANYU'
                      AND p.pinned_at IS NULL
                      AND p.last_crawled_at < :cutoff
                      AND NOT EXISTS (
                          SELECT 1 FROM xianyu_listings xl
                          WHERE xl.product_id = p.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM orders o
                          WHERE o.product_id = p.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM product_matches pm
                          WHERE pm.source_product_id = p.id
                             OR pm.target_product_id = p.id
                      )
                )
                DELETE FROM products WHERE id IN (SELECT id FROM victims)
                """
            ),
            {"cutoff": cutoff},
        )
        await db.commit()
        deleted = result.rowcount or 0
        logger.info(
            f"compliance: daily_purge_collected deleted {deleted} stale xianyu rows "
            f"older than {cutoff.isoformat()} (retention={retention}d)"
        )
        return {
            "purged_at": datetime.now(timezone.utc).isoformat(),
            "cutoff": cutoff.isoformat(),
            "retention_days": retention,
            "deleted": deleted,
        }


# 采集流水保留天数的回落默认值。实际值走运行时配置（前端「数据清理」可改 →
# SystemConfig），读不到时才用这个常量。pdd_search_runs / xianyu_search_runs 同时是
# 「任务记录」的数据源，物理删它会连任务历史一起删，所以按「保留窗口」删而非按逻辑
# 日删——保住最近 N 天任务历史，又给表封顶。收藏的 PDD 快照在独立 pdd_pins 表、闲鱼
# 收藏在 products(pinned_at)，都不受影响。
PDD_RUNS_RETENTION_DAYS = 30
XIANYU_RUNS_RETENTION_DAYS = 30


async def _purge_search_runs(table: str, config_key: str, fallback_days: int) -> dict:
    """通用：按保留天数物理清理某张采集流水表。"""
    async with AsyncSessionLocal() as db:
        try:
            from app.services.pdd_worker_config import get_runtime_config
            cfg = await get_runtime_config(db)
            retention = int(cfg.get(config_key) or fallback_days)
        except Exception:
            retention = fallback_days
        retention = max(1, retention)
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
        result = await db.execute(
            text(f"DELETE FROM {table} WHERE created_at < :cutoff"),
            {"cutoff": cutoff},
        )
        await db.commit()
        deleted = result.rowcount or 0
        logger.info(
            f"compliance: purge {table} deleted {deleted} rows "
            f"older than {cutoff.isoformat()} (retention={retention}d)"
        )
        return {
            "purged_at": datetime.now(timezone.utc).isoformat(),
            "table": table,
            "cutoff": cutoff.isoformat(),
            "retention_days": retention,
            "deleted": deleted,
        }


@celery_app.task(name="app.tasks.compliance.purge_pdd_search_runs")
def purge_pdd_search_runs():
    """每日物理清理过期 PDD 采集流水（保留天数走 pdd_runs_retention_days）。"""
    logger.info("compliance: starting purge_pdd_search_runs")
    return run_async(_purge_search_runs(
        "pdd_search_runs", "pdd_runs_retention_days", PDD_RUNS_RETENTION_DAYS
    ))


@celery_app.task(name="app.tasks.compliance.purge_xianyu_search_runs")
def purge_xianyu_search_runs():
    """每日物理清理过期闲鱼采集流水（保留天数走 xianyu_runs_retention_days）。"""
    logger.info("compliance: starting purge_xianyu_search_runs")
    return run_async(_purge_search_runs(
        "xianyu_search_runs", "xianyu_runs_retention_days", XIANYU_RUNS_RETENTION_DAYS
    ))


# 跨天同款观测表的保留天数。它比流水更想留长一点（看长期价格趋势），单独给个默认
# 90 天；走运行时配置 sightings_retention_days 时可改。
SIGHTINGS_RETENTION_DAYS = 90


@celery_app.task(name="app.tasks.compliance.purge_product_sightings")
def purge_product_sightings():
    """每日物理清理过期跨天同款观测（保留天数走 sightings_retention_days，默认 90）。"""
    logger.info("compliance: starting purge_product_sightings")
    return run_async(_purge_search_runs(
        "product_sightings", "sightings_retention_days", SIGHTINGS_RETENTION_DAYS
    ))
