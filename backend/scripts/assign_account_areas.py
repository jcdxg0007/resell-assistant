"""Assign bound_proxy_area to crawler accounts that still have NULL.

Normally the pool自己会在第一次 pick 时 auto-assign (see
crawler_accounts._assign_area_if_missing), but operators may want to:

- Preview what distribution the pool would end up with before any traffic.
- Reassign areas after adding a new account so the pool re-balances.
- Force a specific area for a named account (e.g. "this号我手动用福建手机
  卡养的，就让它绑福建").

Usage:
    python scripts/assign_account_areas.py                # show current
    python scripts/assign_account_areas.py --auto         # fill NULLs
    python scripts/assign_account_areas.py --set NAME=AREA,NAME2=AREA2
    python scripts/assign_account_areas.py --reset       # null all

Dry-run by default — pass ``--apply`` to persist changes.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from collections import Counter
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.services.crawler_accounts import DEFAULT_ACCOUNT_AREAS


def _raw_db_url() -> str:
    return get_settings().DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://"
    )


async def _fetch_accounts(conn: asyncpg.Connection):
    return await conn.fetch(
        """
        SELECT id, account_name, platform, bound_proxy_area, is_active
        FROM accounts
        WHERE platform LIKE '%_crawler'
        ORDER BY platform, account_name
        """
    )


async def _show(conn: asyncpg.Connection):
    rows = await _fetch_accounts(conn)
    if not rows:
        print("(no crawler accounts)")
        return
    print(f"{'platform':<20} {'account':<30} {'area':<10} active")
    print("-" * 70)
    dist: Counter[str] = Counter()
    for r in rows:
        area = r["bound_proxy_area"] or "-"
        dist[(r["platform"], area)] += 1
        print(
            f"{r['platform']:<20} {r['account_name']:<30} "
            f"{area:<10} {r['is_active']}"
        )
    print("\nDistribution:")
    for (plat, area), n in sorted(dist.items()):
        print(f"  {plat} / {area}: {n}")


async def _auto(conn: asyncpg.Connection, *, apply: bool):
    rows = await _fetch_accounts(conn)
    by_plat_area: dict[tuple[str, str], int] = Counter(
        (r["platform"], r["bound_proxy_area"])
        for r in rows if r["bound_proxy_area"]
    )
    plan: list[tuple[str, str, str, str]] = []
    for r in rows:
        if r["bound_proxy_area"]:
            continue
        # Least-populated area for this account's platform
        counts = [
            (a, by_plat_area.get((r["platform"], a), 0))
            for a in DEFAULT_ACCOUNT_AREAS
        ]
        mn = min(c for _, c in counts)
        candidates = [a for a, c in counts if c == mn]
        area = random.choice(candidates)
        by_plat_area[(r["platform"], area)] += 1
        plan.append((r["id"], r["platform"], r["account_name"], area))

    if not plan:
        print("(all accounts already have bound_proxy_area)")
        return
    print(f"Plan: assign {len(plan)} account(s)")
    for _id, plat, name, area in plan:
        print(f"  [{plat}] {name} → {area}")
    if not apply:
        print("\n(dry-run — rerun with --apply to persist)")
        return
    async with conn.transaction():
        for _id, _plat, _name, area in plan:
            await conn.execute(
                "UPDATE accounts SET bound_proxy_area = $2 WHERE id = $1",
                _id, area,
            )
    print(f"Committed {len(plan)} assignment(s).")


async def _set_explicit(conn: asyncpg.Connection, pairs: str, *, apply: bool):
    items: list[tuple[str, str]] = []
    for chunk in pairs.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise SystemExit(f"bad --set entry: {chunk!r}; expected NAME=AREA")
        name, area = chunk.split("=", 1)
        items.append((name.strip(), area.strip()))

    if not items:
        print("(nothing to set)")
        return

    async with conn.transaction():
        for name, area in items:
            row = await conn.fetchrow(
                "SELECT id, platform FROM accounts WHERE account_name = $1",
                name,
            )
            if not row:
                print(f"  [miss] {name!r}: no such account — skipped")
                continue
            print(f"  [{row['platform']}] {name} → {area}")
            if apply:
                await conn.execute(
                    "UPDATE accounts SET bound_proxy_area = $2 WHERE id = $1",
                    row["id"], area,
                )
    if not apply:
        print("\n(dry-run — rerun with --apply to persist)")


async def _reset(conn: asyncpg.Connection, *, apply: bool):
    if not apply:
        print("(dry-run — rerun with --apply to clear all bound_proxy_area)")
        return
    n = await conn.fetchval(
        "UPDATE accounts SET bound_proxy_area = NULL "
        "WHERE platform LIKE '%_crawler' AND bound_proxy_area IS NOT NULL "
        "RETURNING (SELECT count(*) FROM accounts "
        "          WHERE platform LIKE '%_crawler' "
        "            AND bound_proxy_area IS NOT NULL)"
    )
    # RETURNING subquery runs BEFORE the update, giving the cleared count.
    print(f"Cleared bound_proxy_area on {n or 0} account(s).")


async def main():
    p = argparse.ArgumentParser()
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--auto", action="store_true",
                     help="auto-assign least-populated area to NULL rows")
    grp.add_argument("--set", dest="pairs",
                     help="explicit overrides, e.g. 'pdd_01=350000,pdd_02=330000'")
    grp.add_argument("--reset", action="store_true",
                     help="clear bound_proxy_area on all crawler accounts")
    p.add_argument("--apply", action="store_true",
                   help="actually write changes (otherwise dry-run)")
    args = p.parse_args()

    conn = await asyncpg.connect(_raw_db_url())
    try:
        if args.auto:
            await _auto(conn, apply=args.apply)
        elif args.pairs:
            await _set_explicit(conn, args.pairs, apply=args.apply)
        elif args.reset:
            await _reset(conn, apply=args.apply)
        else:
            await _show(conn)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
