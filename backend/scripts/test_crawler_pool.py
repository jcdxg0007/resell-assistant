"""Smoke test for the crawler-pool rotation + quarantine logic.

Inserts two fake crawler accounts into the 'pdd_crawler' pool, then
simulates a sequence of pick / report calls:

  round 1: pick → A (LRU among never-used)
  round 2: pick → B
  round 3: mark A burnt → cooldown_until = now + 60m
  round 4: pick → B again (A is in cooldown)
  round 5: mark B success → last_used_at bumps
  round 6: pick → B (A still cooling, B is still the only candidate)

Prints a trace and cleans up the fakes at the end. Run:

    DATABASE_URL=... python3 scripts/test_crawler_pool.py

No prod data is touched — everything is scoped by account_name prefix
``_pooltest_`` and rolled back at the end.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncpg

from app.core.config import get_settings
from app.services.crawler_accounts import (
    _pick_async, _report_async, COOLDOWN_MINUTES,
)


FAKE_TAG = "pooltest"  # so pool queries become "pooltest_crawler"
FAKE_PLATFORM = f"{FAKE_TAG}_crawler"
FAKE_PREFIX = "_pooltest_"
FAKE_COOKIES = json.dumps({
    "cookies": [{"name": "x", "value": "1", "domain": ".example.com", "path": "/"}],
    "origins": [],
})


async def _seed(conn: asyncpg.Connection, names: list[str]) -> list[str]:
    ids = []
    for n in names:
        row = await conn.fetchrow(
            """
            INSERT INTO accounts
            (id, platform, account_name, identity_group, lifecycle_stage,
             daily_publish_limit, daily_published_count,
             is_active, session_status, cookies_data, health_score,
             created_at, updated_at)
            VALUES (gen_random_uuid(), $1, $2, 'pooltest', 'mature',
                    0, 0,
                    true, 'active', $3, 100,
                    now(), now())
            RETURNING id
            """,
            FAKE_PLATFORM, f"{FAKE_PREFIX}{n}", FAKE_COOKIES,
        )
        ids.append(str(row["id"]))
    return ids


async def _cleanup(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "DELETE FROM accounts WHERE account_name LIKE $1",
        f"{FAKE_PREFIX}%",
    )


async def _snapshot(conn: asyncpg.Connection) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT account_name, last_used_at, cooldown_until, health_score
        FROM accounts
        WHERE account_name LIKE $1
        ORDER BY account_name
        """,
        f"{FAKE_PREFIX}%",
    )
    return [dict(r) for r in rows]


def _print_snap(label: str, snap: list[dict]) -> None:
    print(f"\n=== {label} ===")
    now = datetime.now(timezone.utc)
    for r in snap:
        cd = r["cooldown_until"]
        if cd and cd.tzinfo is None:
            cd = cd.replace(tzinfo=timezone.utc)
        cd_str = (
            f"cooldown {(cd - now).total_seconds() / 60:+.1f}m"
            if cd else "ready"
        )
        print(
            f"  {r['account_name']:<24}  "
            f"last_used={r['last_used_at']}  hp={r['health_score']:.0f}  {cd_str}"
        )


async def run() -> None:
    raw_url = get_settings().DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    conn = await asyncpg.connect(raw_url)
    try:
        await _cleanup(conn)
        ids = await _seed(conn, ["A", "B"])
        id_a, id_b = ids
        print(f"Seeded fakes A={id_a} B={id_b}")
        _print_snap("after seed", await _snapshot(conn))

        # round 1 — pick should grab A (NULLS FIRST + alphabetical via
        # health-score tie-break: both 100, A inserted first so its
        # hidden row order gives it priority — we assert loosely).
        pick1 = await _pick_async(FAKE_TAG)
        assert pick1, "round 1 pick returned None"
        pick1_id, pick1_name, *_ = pick1
        print(f"\nround 1 pick → {pick1_name} ({pick1_id})")
        _print_snap("after round 1", await _snapshot(conn))

        # round 2 — pick should grab the other one (the first is now
        # last_used_at=now, so the NULLS FIRST sort promotes the other).
        pick2 = await _pick_async(FAKE_TAG)
        assert pick2, "round 2 pick returned None"
        pick2_id, pick2_name, *_ = pick2
        print(f"round 2 pick → {pick2_name} ({pick2_id})")
        assert pick2_id != pick1_id, (
            f"rotation broken: round 2 returned same id {pick2_id}"
        )
        _print_snap("after round 2 (both used)", await _snapshot(conn))

        # round 3 — mark pick1 burnt
        print(f"\nround 3 → mark {pick1_name} burnt (empty_result)")
        await _report_async(pick1_id, burnt=True, reason="test_empty_result")
        _print_snap("after burn", await _snapshot(conn))

        # round 4 — pick should skip pick1 (in cooldown) and return pick2
        pick4 = await _pick_async(FAKE_TAG)
        assert pick4, "round 4 pick returned None despite one healthy account"
        pick4_id, pick4_name, *_ = pick4
        print(f"\nround 4 pick (A in cooldown) → {pick4_name}")
        assert pick4_id == pick2_id, (
            f"quarantine broken: picked {pick4_name} but {pick1_name} "
            f"should be in cooldown"
        )

        # round 5 — mark pick2 success
        print(f"\nround 5 → mark {pick2_name} success")
        await _report_async(pick2_id, burnt=False)
        _print_snap("after success", await _snapshot(conn))

        # round 6 — both are now "used", but pick1 is still in cooldown
        # so pick should still return pick2.
        pick6 = await _pick_async(FAKE_TAG)
        assert pick6, "round 6 pick returned None"
        pick6_id, pick6_name, *_ = pick6
        print(f"\nround 6 pick (A cooling, B only candidate) → {pick6_name}")
        assert pick6_id == pick2_id, (
            f"expected {pick2_name} since A is still in cooldown"
        )

        # round 7 — force-clear A's cooldown and verify rotation resumes
        print(f"\nround 7 → manually clear {pick1_name}'s cooldown")
        await conn.execute(
            "UPDATE accounts SET cooldown_until = NULL WHERE id = $1",
            pick1_id,
        )
        pick7 = await _pick_async(FAKE_TAG)
        pick7_id, pick7_name, *_ = pick7  # type: ignore[misc]
        print(f"round 7 pick → {pick7_name}")
        assert pick7_id == pick1_id, (
            "rotation didn't resume after manual unquarantine"
        )

        print("\n✅ All pool-rotation + quarantine invariants hold.")
        print(f"   (cooldown length configured: {COOLDOWN_MINUTES} minutes)")
    finally:
        await _cleanup(conn)
        await conn.close()


if __name__ == "__main__":
    if "DATABASE_URL" not in os.environ:
        print(
            "⚠️  DATABASE_URL not set — export it before running this test.\n"
            "    e.g. DATABASE_URL='postgresql+asyncpg://user:pw@host/db'",
            file=sys.stderr,
        )
        sys.exit(2)
    asyncio.run(run())
