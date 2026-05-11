"""
Crawler-compliance policy centre.

Codifies legal / operational constraints that apply to *every* crawler
call in the system. These rules are **not** advisory — every crawler
entry point is required to pass through ``compliance_gate`` before
issuing a platform request. Bypassing this module is a policy
violation, not a performance optimisation.

Four hard rules (see ``docs/compliance.md`` for rationale):

1. **Minimum 60s between same-platform calls.** No platform is hit more
   than once per minute, regardless of which worker/task initiates the
   call. Enforced via Redis "last-call" timestamp (shared across all
   Celery workers).

2. **Randomised inter-call jitter.** When the gate lets a call through
   it still sleeps a random extra window (default 5-25s) to break any
   residual "on the minute" pattern. Platforms fingerprint regular
   callers; we want the traffic shape to look like a human operator.

3. **Human-active hours only.** Automated crawling is restricted to
   Beijing-time 08:00-23:00. Requests outside this window are denied.
   (Instant-search UI calls made by a logged-in operator still go
   through — those are authentically human.)

4. **Product library soft cap: 100 000 rows.** The ``products`` table
   is rotated FIFO by ``last_crawled_at`` once the cap is exceeded; see
   ``enforce_product_cap`` in ``app.tasks.compliance``.

Integration points:

    from app.services.compliance import compliance_gate

    allowed, reason = await compliance_gate("pdd", actor="scheduled")
    if not allowed:
        # skip / reschedule; never bypass
        return

The ``actor`` argument lets the gate relax rule 3 for user-triggered
instant searches (``actor="user"``) while keeping the strict version
active for Celery beat tasks (``actor="scheduled"``). Rules 1, 2, 4
apply to **all** actors without exception.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Literal

from loguru import logger

from app.core.config import get_settings


# Redis keys
_LAST_CALL_PREFIX = "compliance:last_call:"
_MIN_WAIT_LOCK_PREFIX = "compliance:pacing_lock:"

Actor = Literal["user", "scheduled", "internal"]


@dataclass
class GateDecision:
    allowed: bool
    reason: str
    wait_seconds: float = 0.0

    def __bool__(self) -> bool:
        return self.allowed


# ─── rule 1 + 2: pacing ───────────────────────────────────────────────

async def compliance_gate(
    platform: str,
    actor: Actor = "scheduled",
    sleep_if_blocked: bool = True,
    max_wait_seconds: float | None = None,
) -> GateDecision:
    """Single entry-point that all crawler calls **must** traverse.

    Enforces rules 1-3 in one round-trip:

    - rule 1 (min interval): if <60s since the last same-platform call,
      either sleep until the minute is up (when ``sleep_if_blocked=True``)
      or return ``allowed=False``.
    - rule 2 (jitter): after the 60s floor, sleep an extra random
      window so we're never exactly on-the-minute.
    - rule 3 (active hours): if the current Beijing hour is outside
      ``COMPLIANCE_ACTIVE_HOURS_BEIJING`` *and* ``actor != "user"``,
      the call is denied.

    ``max_wait_seconds`` caps how long we'll wait; defaults to 5 minutes.
    If reached we give up and return ``allowed=False`` so the caller can
    choose to skip rather than block a Celery worker indefinitely.
    """
    settings = get_settings()

    # rule 3 — active hours (user-triggered calls exempt)
    if actor != "user":
        ok, hrs_reason = _check_active_hours()
        if not ok:
            logger.info(
                f"compliance: blocking {actor} {platform} call "
                f"(outside active hours): {hrs_reason}"
            )
            return GateDecision(
                allowed=False, reason=f"outside_active_hours: {hrs_reason}"
            )

    min_interval = settings.COMPLIANCE_MIN_INTERVAL_SECONDS
    jitter_lo = settings.COMPLIANCE_JITTER_MIN_SECONDS
    jitter_hi = settings.COMPLIANCE_JITTER_MAX_SECONDS
    ceiling = max_wait_seconds if max_wait_seconds is not None else 300.0

    from app.services.proxy_service import _PerCallRedis
    key = f"{_LAST_CALL_PREFIX}{platform}"
    lock_key = f"{_MIN_WAIT_LOCK_PREFIX}{platform}"

    total_waited = 0.0
    async with _PerCallRedis() as r:
        # Use a short Redis lock so two concurrent celery workers can't
        # both think "ok, 60s is up" and fire at the same instant.
        # The lock itself is best-effort (NX+EX), not a distributed-lock
        # library, but it's enough to serialise the gate.
        while True:
            now_ts = time.time()
            raw_last = await r.get(key)
            last_ts = float(raw_last) if raw_last else 0.0
            elapsed = now_ts - last_ts
            wait_needed = max(0.0, min_interval - elapsed)

            if wait_needed <= 0:
                # Try to claim the slot — if another worker grabs it
                # first, loop back and wait again.
                got = await r.set(
                    lock_key, "1", ex=max(2, int(min_interval // 6)), nx=True
                )
                if got:
                    await r.set(key, str(now_ts), ex=int(min_interval * 4))
                    break
                # Lost the race; wait briefly and retry.
                wait_needed = random.uniform(0.5, 2.0)

            if not sleep_if_blocked:
                return GateDecision(
                    allowed=False,
                    reason="min_interval_not_elapsed",
                    wait_seconds=wait_needed,
                )

            if total_waited + wait_needed > ceiling:
                logger.warning(
                    f"compliance: {platform} gate wait exceeded {ceiling:.0f}s, "
                    f"giving up"
                )
                return GateDecision(
                    allowed=False,
                    reason="wait_ceiling_exceeded",
                    wait_seconds=total_waited,
                )

            logger.info(
                f"compliance: {platform} pacing — sleeping {wait_needed:.1f}s "
                f"(last call {elapsed:.1f}s ago, actor={actor})"
            )
            await asyncio.sleep(wait_needed)
            total_waited += wait_needed

    # rule 2 — jitter on top of the floor so we're not exactly on the minute
    jitter = random.uniform(jitter_lo, jitter_hi)
    logger.debug(
        f"compliance: {platform} passed pacing, jittering +{jitter:.1f}s "
        f"(total waited {total_waited + jitter:.1f}s)"
    )
    await asyncio.sleep(jitter)

    return GateDecision(
        allowed=True,
        reason="ok",
        wait_seconds=total_waited + jitter,
    )


# ─── rule 3: active-hours check ───────────────────────────────────────

def _check_active_hours(now_utc: datetime | None = None) -> tuple[bool, str]:
    """Return (allowed, reason). Active window is Beijing local time."""
    settings = get_settings()
    start_h, end_h = _parse_active_hours(settings.COMPLIANCE_ACTIVE_HOURS_BEIJING)
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    # Beijing is UTC+8 (no DST).
    beijing = now_utc + timedelta(hours=8)
    hr = beijing.hour
    if start_h <= end_h:
        ok = start_h <= hr < end_h
    else:
        # e.g. 22-06 window crosses midnight
        ok = hr >= start_h or hr < end_h
    return ok, f"beijing_hour={hr}, window={start_h}-{end_h}"


def _parse_active_hours(spec: str) -> tuple[int, int]:
    """'8-23' → (8, 23). Tolerates whitespace."""
    try:
        lo, hi = spec.strip().split("-")
        return int(lo), int(hi)
    except Exception:
        logger.warning(
            f"compliance: malformed COMPLIANCE_ACTIVE_HOURS_BEIJING='{spec}', "
            f"falling back to 8-23"
        )
        return 8, 23


def is_active_hours(now_utc: datetime | None = None) -> bool:
    """Convenience wrapper for callers that only need the bool."""
    ok, _ = _check_active_hours(now_utc)
    return ok


# ─── rule 4: product-library cap (logic; Celery task is in app.tasks) ─

def product_library_cap() -> int:
    return get_settings().PRODUCT_LIBRARY_CAP


# ─── diagnostics helper (used by the admin UI later) ──────────────────

async def pacing_status(platform: str) -> dict:
    """Return a snapshot of the compliance-gate state for ``platform``.

    Useful for UI dashboards and debugging — never touches the counter.
    """
    from app.services.proxy_service import _PerCallRedis
    key = f"{_LAST_CALL_PREFIX}{platform}"
    async with _PerCallRedis() as r:
        raw = await r.get(key)
    last_ts = float(raw) if raw else 0.0
    settings = get_settings()
    elapsed = time.time() - last_ts if last_ts else None
    return {
        "platform": platform,
        "last_call_ts": last_ts or None,
        "elapsed_seconds": elapsed,
        "min_interval_seconds": settings.COMPLIANCE_MIN_INTERVAL_SECONDS,
        "ready_in_seconds": (
            max(0.0, settings.COMPLIANCE_MIN_INTERVAL_SECONDS - (elapsed or 0))
            if elapsed is not None else 0.0
        ),
        "active_hours_now": is_active_hours(),
    }
