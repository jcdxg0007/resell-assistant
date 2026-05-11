"""Human-like behavior injection for Playwright pages.

Anti-bot SDKs like 风神 (Alibaba) and 拼多多 prowler score sessions on
"interaction curve" features that distinguish automation from humans:

- Mouse coordinates form a smooth Bezier curve vs straight line.
- Time between pageload and first interaction is 1-5 s for humans, <100ms
  for naive crawlers.
- Scroll events come in bursty chunks (wheel + pause + wheel) vs single
  programmatic scrollTo jumps.
- Key-press dwell / flight times follow a log-normal distribution.

Programmatic automation leaves all of these empty or uniform. We fix it
with a reusable ``humanize_page(page)`` that:

  1. Waits a realistic "landing pause" (1-4 s).
  2. Drifts the mouse along a curved path across the viewport.
  3. Wheel-scrolls in bursts of 2-5 ticks with inter-burst pauses.

Safe to call multiple times; each invocation adds fresh entropy. Design
constraint: never blocks longer than ~6 s total — we already pay a
compliance jitter (5-25 s) before touching the site, this is on top.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any

from loguru import logger


async def _sleep_jitter(low_ms: int, high_ms: int) -> None:
    await asyncio.sleep(random.uniform(low_ms, high_ms) / 1000.0)


async def _bezier_mouse_drift(page: Any, viewport: dict[str, int]) -> None:
    """Move the mouse along a quadratic Bezier curve.

    Two random control points → smooth, non-straight path that matches
    cursor trails real users produce when looking around the page.
    """
    w = viewport.get("width", 1280)
    h = viewport.get("height", 720)

    p0 = (random.randint(50, w - 50), random.randint(50, h - 50))
    p1 = (random.randint(50, w - 50), random.randint(50, h - 50))
    p2 = (random.randint(50, w - 50), random.randint(50, h - 50))

    steps = random.randint(20, 35)
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
        try:
            await page.mouse.move(x, y, steps=1)
        except Exception:
            return
        await asyncio.sleep(random.uniform(0.005, 0.025))


async def _burst_wheel(page: Any, total_px: int = 600) -> None:
    """Scroll in 2-4 bursts of wheel ticks with inter-burst pauses.

    Real users rarely smooth-scroll a fixed delta — they nudge, pause,
    nudge again. Emulate that instead of programmatic scrollTo.
    """
    bursts = random.randint(2, 4)
    per_burst = total_px // bursts
    for b in range(bursts):
        ticks = random.randint(3, 6)
        per_tick = per_burst // ticks
        for _ in range(ticks):
            try:
                await page.mouse.wheel(0, per_tick + random.randint(-15, 15))
            except Exception:
                return
            await asyncio.sleep(random.uniform(0.03, 0.09))
        # Inter-burst pause: mid-viewport reading time.
        await _sleep_jitter(400, 1200)


async def humanize_page(
    page: Any,
    *,
    scroll_px: int = 600,
    include_drift: bool = True,
    include_scroll: bool = True,
) -> None:
    """Perform a short sequence of human-like interactions on ``page``.

    Typical placement: right after ``page.goto(...)`` completes and
    before the crawler does its structured data extraction. Total cost
    is bounded (~1-6 s) and each phase is try/except isolated so a
    failure never bubbles up into the crawler logic — worst case you
    get slightly less believable behavior, never a broken crawl.
    """
    try:
        # 1. Landing pause — human eye takes ~1-4 s to orient.
        await _sleep_jitter(1000, 3500)

        # 2. Viewport-based curve path.
        if include_drift:
            viewport = page.viewport_size or {"width": 1280, "height": 720}
            try:
                await _bezier_mouse_drift(page, viewport)
            except Exception as e:
                logger.debug(f"humanize: mouse drift skipped ({e})")

        # 3. Bursty wheel scrolling.
        if include_scroll and scroll_px > 0:
            try:
                await _burst_wheel(page, total_px=scroll_px)
            except Exception as e:
                logger.debug(f"humanize: wheel scroll skipped ({e})")

        # 4. Settle pause before the crawler reads DOM.
        await _sleep_jitter(300, 900)
    except Exception as e:
        logger.warning(f"humanize_page silently degraded: {e}")
