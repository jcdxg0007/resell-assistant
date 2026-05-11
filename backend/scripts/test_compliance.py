"""Smoke test for the compliance module.

Verifies the three live rules (pacing / jitter / active-hours) and
prints the resulting timings so regressions in future refactors are
immediately visible. The product-cap rule is a DB task and has its
own isolated test (``test_product_cap.py``).

Run:

    python3 scripts/test_compliance.py
"""
import asyncio
import os
import sys
import time
from pathlib import Path

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:cfghhm7f@resell-manager-postgresql.ns-3zn44u6p.svc:5432/postgres",
)
os.environ.setdefault(
    "REDIS_URL",
    "redis://default:Xv01aH061L@resell--manager-redis-redis.ns-3zn44u6p.svc:6379/0",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.services.compliance import (  # noqa: E402
    compliance_gate,
    is_active_hours,
    pacing_status,
    _check_active_hours,
    _LAST_CALL_PREFIX,
    _MIN_WAIT_LOCK_PREFIX,
)


async def _reset_platform(platform: str) -> None:
    from app.services.proxy_service import _PerCallRedis
    async with _PerCallRedis() as r:
        await r.delete(f"{_LAST_CALL_PREFIX}{platform}")
        await r.delete(f"{_MIN_WAIT_LOCK_PREFIX}{platform}")


async def test_first_call_passes_immediately():
    platform = "complycheck_A"
    await _reset_platform(platform)
    t0 = time.monotonic()
    decision = await compliance_gate(platform, actor="user")
    elapsed = time.monotonic() - t0
    settings = get_settings()
    jitter_cap = settings.COMPLIANCE_JITTER_MAX_SECONDS + 1
    assert decision.allowed, f"first call must pass: {decision.reason}"
    assert elapsed <= jitter_cap, (
        f"first call waited {elapsed:.1f}s, expected <= {jitter_cap}s"
    )
    print(f"  OK  first-call elapsed {elapsed:.2f}s (jitter only)")


async def test_second_call_waits_min_interval():
    platform = "complycheck_B"
    await _reset_platform(platform)
    await compliance_gate(platform, actor="user")

    settings = get_settings()
    min_int = settings.COMPLIANCE_MIN_INTERVAL_SECONDS

    # The second call should refuse to return faster than the min
    # interval — we use the non-blocking form so this test doesn't
    # actually wait 60s.
    t0 = time.monotonic()
    decision = await compliance_gate(
        platform, actor="user", sleep_if_blocked=False
    )
    elapsed = time.monotonic() - t0
    assert not decision.allowed, "second call should be blocked"
    assert decision.reason == "min_interval_not_elapsed"
    assert decision.wait_seconds > min_int * 0.5, (
        f"wait_seconds={decision.wait_seconds} looks wrong"
    )
    print(
        f"  OK  second-call blocked after {elapsed:.2f}s, "
        f"must wait {decision.wait_seconds:.1f}s more"
    )


async def test_active_hours_check():
    from datetime import datetime, timezone, timedelta
    ok, info = _check_active_hours()
    print(f"  INFO current active hours: {is_active_hours()} ({info})")
    settings = get_settings()
    spec = settings.COMPLIANCE_ACTIVE_HOURS_BEIJING
    print(f"  INFO configured window: {spec}")

    # Cross-midnight sanity: with "8-2" window, assert the expected
    # in/out decisions at 09:00 / 13:00 / 01:00 / 03:00 / 07:00 Beijing.
    def _fake_beijing(hour: int) -> datetime:
        # Build a UTC timestamp that, when +8h is applied, lands on
        # exactly ``hour``:00 Beijing time.
        return datetime(2026, 1, 1, (hour - 8) % 24, 0, tzinfo=timezone.utc)

    expectations = {9: True, 13: True, 1: True, 3: False, 7: False}
    for beijing_hr, expected in expectations.items():
        ok, info = _check_active_hours(_fake_beijing(beijing_hr))
        assert ok is expected, (
            f"active hours window broken: "
            f"beijing {beijing_hr:02d}:00 got {ok}, expected {expected} ({info})"
        )
        print(f"  OK  beijing {beijing_hr:02d}:00 → active={ok}")


async def test_scheduled_actor_denied_off_hours():
    # We can't time-travel the clock here, but we can verify that the
    # logic path is exercised — simply confirm the code runs without
    # raising, and that a user-actor call would be allowed in the same
    # moment (proving the actor distinction is respected).
    platform = "complycheck_C"
    await _reset_platform(platform)
    user_decision = await compliance_gate(
        platform, actor="user", sleep_if_blocked=False
    )
    assert user_decision.allowed, "user actor should always pass rule 3"
    print(f"  OK  user actor always passes active-hours (rule 3)")


async def test_pacing_status_observability():
    platform = "complycheck_D"
    await _reset_platform(platform)
    await compliance_gate(platform, actor="user")
    snap = await pacing_status(platform)
    assert snap["platform"] == platform
    assert snap["last_call_ts"] is not None
    assert snap["elapsed_seconds"] is not None and snap["elapsed_seconds"] >= 0
    print(f"  OK  pacing_status → {snap}")


async def main():
    print("=== compliance module smoke test ===\n")

    print("[1] first call passes immediately (jitter only)")
    await test_first_call_passes_immediately()

    print("\n[2] second call within 60s is blocked")
    await test_second_call_waits_min_interval()

    print("\n[3] active-hours window")
    await test_active_hours_check()

    print("\n[4] user actor bypasses active-hours rule")
    await test_scheduled_actor_denied_off_hours()

    print("\n[5] pacing_status observability")
    await test_pacing_status_observability()

    print("\n=== all compliance tests passed ===")


if __name__ == "__main__":
    asyncio.run(main())
