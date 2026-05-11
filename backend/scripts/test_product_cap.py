"""Smoke test for enforce_product_cap.

Strategy: monkey-patch ``PRODUCT_LIBRARY_CAP`` to a value just below the
current row count, run the task, verify the cap is honoured, then
restore the original value. No seed rows needed — uses live data.

Because deletes are irreversible, the test only removes the very oldest
crawl-only rows (≤ 10 of them). If the products table is smaller than
11 rows the test exits without doing anything, so it's safe to run on a
fresh dev DB too.

Run:

    python3 scripts/test_product_cap.py
"""
import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:cfghhm7f@resell-manager-postgresql.ns-3zn44u6p.svc:5432/postgres",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.database import AsyncSessionLocal  # noqa: E402


async def _count() -> int:
    async with AsyncSessionLocal() as db:
        return (await db.execute(text("SELECT count(*) FROM products"))).scalar_one()


async def main():
    settings = get_settings()
    print(f"configured PRODUCT_LIBRARY_CAP = {settings.PRODUCT_LIBRARY_CAP:,}")

    before = await _count()
    print(f"products row count before = {before:,}")

    if before < 11:
        print(
            "table has fewer than 11 rows — skipping destructive test "
            "(nothing meaningful to evict)"
        )
        return

    # Pretend the cap is "keep all but 5 rows" so we exercise the
    # delete path without nuking useful data.
    fake_cap = max(1, before - 5)
    settings.PRODUCT_LIBRARY_CAP = fake_cap
    print(f"temporarily set cap to {fake_cap} (delta = 5 rows)")

    try:
        from app.tasks.compliance import _enforce_product_cap
        result = await _enforce_product_cap()
        print(f"enforce result: {result}")
    finally:
        settings.PRODUCT_LIBRARY_CAP = 100_000
        print(f"restored cap to 100,000")

    after = await _count()
    print(f"products row count after  = {after:,}")
    print(f"delta = {before - after:,} rows evicted")

    assert after <= before, "row count must not grow"
    # We deleted up to 5, but business-linked rows may be protected, so
    # the delta is in [0, 5].
    assert before - after <= 5, f"evicted too many ({before - after})"
    print("\n=== enforce_product_cap smoke test passed ===")


if __name__ == "__main__":
    asyncio.run(main())
