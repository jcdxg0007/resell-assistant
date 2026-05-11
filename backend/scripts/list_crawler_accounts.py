"""Print the current state of every crawler-pool account.

Usage:
    python scripts/list_crawler_accounts.py              # all platforms
    python scripts/list_crawler_accounts.py pdd          # just pdd
    python scripts/list_crawler_accounts.py --unquarantine <account_id>
                                                         # force-clear cooldown

Columns:
    platform                 pdd_crawler / 1688_crawler / ...
    account_name             human-readable label
    session_status           none / active / expired
    health_score             0..100 (bumped ±1/5 per run)
    last_used_at             UTC timestamp, NULL = never picked
    cooldown_until           UTC timestamp, NULL = available
    minutes_until_ready      negative → available, positive → cooling down
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncpg

from app.core.config import get_settings


def _fmt(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


async def _list(platform_filter: str | None) -> None:
    raw_url = get_settings().DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    conn = await asyncpg.connect(raw_url)
    try:
        if platform_filter:
            rows = await conn.fetch(
                """
                SELECT platform, account_name, session_status, is_active,
                       health_score, last_used_at, cooldown_until,
                       suspended_reason, id
                FROM accounts
                WHERE platform = $1
                ORDER BY last_used_at NULLS FIRST
                """,
                f"{platform_filter}_crawler",
            )
        else:
            rows = await conn.fetch(
                """
                SELECT platform, account_name, session_status, is_active,
                       health_score, last_used_at, cooldown_until,
                       suspended_reason, id
                FROM accounts
                WHERE platform LIKE '%_crawler'
                ORDER BY platform, last_used_at NULLS FIRST
                """
            )
    finally:
        await conn.close()

    if not rows:
        print("No crawler accounts found.")
        return

    now = datetime.now(timezone.utc)
    header = (
        f"{'platform':<16}{'account_name':<26}{'status':<9}{'hp':>5}  "
        f"{'last_used_at':<18}{'cooldown_until':<18}{'min_ready':>10}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        cd = r["cooldown_until"]
        if cd is None:
            mins = "ready"
        else:
            if cd.tzinfo is None:
                cd = cd.replace(tzinfo=timezone.utc)
            delta = (cd - now).total_seconds() / 60
            mins = f"{delta:+.1f}" if delta > 0 else "ready"
        active_flag = "" if r["is_active"] else "*inactive*"
        print(
            f"{r['platform']:<16}"
            f"{r['account_name'][:25]:<26}"
            f"{r['session_status']:<9}"
            f"{r['health_score']:>5.0f}  "
            f"{_fmt(r['last_used_at']):<18}"
            f"{_fmt(r['cooldown_until']):<18}"
            f"{mins:>10}"
            f" {active_flag}"
        )


async def _clear_cooldown(account_id: str) -> None:
    raw_url = get_settings().DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    conn = await asyncpg.connect(raw_url)
    try:
        result = await conn.execute(
            "UPDATE accounts SET cooldown_until = NULL, "
            "suspended_reason = NULL WHERE id = $1",
            account_id,
        )
        print(f"Cleared cooldown: {result}")
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "platform", nargs="?",
        help="filter by platform tag (e.g. 'pdd' → 'pdd_crawler')",
    )
    parser.add_argument(
        "--unquarantine", metavar="ACCOUNT_ID",
        help="force-clear cooldown_until for the given account id",
    )
    args = parser.parse_args()

    os.environ.setdefault(
        "DATABASE_URL",
        os.environ.get("DATABASE_URL")
        or "postgresql+asyncpg://postgres:postgres@localhost:5432/xianyu",
    )

    if args.unquarantine:
        asyncio.run(_clear_cooldown(args.unquarantine))
        return
    asyncio.run(_list(args.platform))


if __name__ == "__main__":
    main()
