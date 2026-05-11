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

from datetime import datetime, timezone

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
