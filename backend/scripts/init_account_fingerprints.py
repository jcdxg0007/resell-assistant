"""Backfill ``Account.fingerprint`` for accounts that have NULL or a
stale (pre-V3) shape.

Why this exists:
    V3 stores a per-account stable hardware profile in the existing
    ``Account.fingerprint`` JSON column. The runtime path (browser.py)
    creates one lazily on first context start, but we want operators to
    be able to:

    1. Preview the distribution before any traffic touches the cluster
       (so we can confirm we're not concentrated on one hw profile).
    2. One-shot backfill all accounts so the *first* crawl after deploy
       isn't paying a DB write on the hot path.
    3. Inspect what each account looks like ("show me 0043 fingerprint").
    4. Force-reroll a single account (e.g. its profile turned out to be
       too rare and is itself a signal).

Usage::

    python scripts/init_account_fingerprints.py                # show
    python scripts/init_account_fingerprints.py --auto         # backfill
    python scripts/init_account_fingerprints.py --reroll NAME  # force regen
    python scripts/init_account_fingerprints.py --reset        # null all

Dry-run unless ``--apply`` is passed for mutating actions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.services.account_fingerprint import (  # noqa: E402
    CURRENT_VERSION,
    _generate_fingerprint,
    _ensure_shape,
)


def _raw_db_url() -> str:
    return get_settings().DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://"
    )


async def _fetch_all(conn) -> list[dict]:
    rows = await conn.fetch(
        "SELECT id, account_name, platform, fingerprint "
        "FROM accounts ORDER BY platform, account_name"
    )
    return [dict(r) for r in rows]


def _parse_fp(raw) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def _needs_init(fp: dict | None) -> bool:
    if not fp:
        return True
    if fp.get("version") != CURRENT_VERSION:
        return True
    if "hardware_concurrency" not in fp:
        return True
    return False


async def _cmd_show(conn) -> None:
    rows = await _fetch_all(conn)
    if not rows:
        print("(no accounts)")
        return

    hw_counter: Counter[int] = Counter()
    mem_counter: Counter[int] = Counter()
    screen_counter: Counter[str] = Counter()
    missing = 0
    print(f"{'name':<28}  {'platform':<14}  hw  mem  screen           plat")
    print("-" * 90)
    for r in rows:
        fp = _parse_fp(r["fingerprint"])
        if _needs_init(fp):
            missing += 1
            print(
                f"{r['account_name']:<28}  {r['platform']:<14}  "
                f"(uninitialised)"
            )
            continue
        scr = fp.get("screen") or {}
        s_str = f"{scr.get('width','?')}x{scr.get('height','?')}"
        print(
            f"{r['account_name']:<28}  {r['platform']:<14}  "
            f"{fp.get('hardware_concurrency',0):<3} "
            f"{fp.get('device_memory',0):<3} "
            f"{s_str:<16} {fp.get('platform_str','?')}"
        )
        hw_counter[fp.get("hardware_concurrency", 0)] += 1
        mem_counter[fp.get("device_memory", 0)] += 1
        screen_counter[s_str] += 1
    print()
    print(f"total: {len(rows)}, uninitialised: {missing}")
    print(f"hw_concurrency distribution: {dict(hw_counter)}")
    print(f"device_memory  distribution: {dict(mem_counter)}")
    print(f"screen         distribution: {dict(screen_counter)}")


async def _cmd_auto(conn, apply: bool) -> None:
    rows = await _fetch_all(conn)
    todo = [
        r for r in rows if _needs_init(_parse_fp(r["fingerprint"]))
    ]
    if not todo:
        print("nothing to do — all accounts already have a v1 fingerprint.")
        return
    print(f"will initialise fingerprint for {len(todo)} accounts:")
    for r in todo:
        fp = _ensure_shape(_parse_fp(r["fingerprint"]))
        print(
            f"  {r['account_name']:<28} -> "
            f"hw={fp['hardware_concurrency']}, "
            f"mem={fp['device_memory']}, "
            f"screen={fp['screen']['width']}x{fp['screen']['height']}, "
            f"platform={fp['platform_str']}"
        )
        if apply:
            await conn.execute(
                "UPDATE accounts SET fingerprint = $1::jsonb WHERE id = $2",
                json.dumps(fp), r["id"],
            )
    if apply:
        print(f"\napplied: {len(todo)} rows updated.")
    else:
        print("\ndry-run only. add --apply to persist.")


async def _cmd_reroll(conn, name: str, apply: bool) -> None:
    row = await conn.fetchrow(
        "SELECT id, fingerprint FROM accounts WHERE account_name = $1", name
    )
    if not row:
        print(f"account not found: {name}")
        return
    existing = _parse_fp(row["fingerprint"]) or {}
    # Preserve frozen_cookies if any — only reroll the hardware shape.
    new_fp = _generate_fingerprint()
    new_fp["frozen_cookies"] = existing.get("frozen_cookies", {})
    print(f"new fingerprint for {name}:")
    print(json.dumps(new_fp, indent=2, ensure_ascii=False))
    if apply:
        await conn.execute(
            "UPDATE accounts SET fingerprint = $1::jsonb WHERE id = $2",
            json.dumps(new_fp), row["id"],
        )
        print("applied.")
    else:
        print("\ndry-run only. add --apply to persist.")


async def _cmd_reset(conn, apply: bool) -> None:
    rows = await conn.fetch(
        "SELECT id, account_name FROM accounts WHERE fingerprint IS NOT NULL"
    )
    print(f"will null fingerprint for {len(rows)} accounts.")
    if apply:
        await conn.execute("UPDATE accounts SET fingerprint = NULL")
        print("applied.")
    else:
        print("dry-run only. add --apply to persist.")


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auto", action="store_true",
                        help="initialise fingerprint for NULL/stale accounts")
    parser.add_argument("--reroll", metavar="NAME",
                        help="force-regenerate fingerprint for one account")
    parser.add_argument("--reset", action="store_true",
                        help="null all fingerprints (dangerous — debug only)")
    parser.add_argument("--apply", action="store_true",
                        help="actually persist changes (default: dry-run)")
    args = parser.parse_args()

    conn = await asyncpg.connect(_raw_db_url())
    try:
        if args.reset:
            await _cmd_reset(conn, args.apply)
        elif args.reroll:
            await _cmd_reroll(conn, args.reroll, args.apply)
        elif args.auto:
            await _cmd_auto(conn, args.apply)
        else:
            await _cmd_show(conn)
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
