"""
Anti-risk utilities for crawler traffic shaping and early warning.

Three jobs:

1. **Traffic shaping** (:func:`human_delay`, :func:`scroll_like_human`,
   :func:`simulate_detail_dwell`): make Playwright navigation look less
   like a headless scraper. Randomised delays, non-uniform scroll
   distances, and occasional detail-page dwell.

2. **Rate limiting** (:func:`rate_limit_guard`): Redis-backed sliding
   window so crawler tasks can't accidentally hammer a platform beyond
   the configured per-hour quota. Shared across Celery workers.

3. **Risk signal collection + DingTalk alerting** (:class:`RiskSignal`,
   :func:`detect_risk_in_page`, :func:`flush_risk_alerts`): crawlers
   report matches of "验证码 / 登录 / 异常访问" patterns; the orchestrator
   batches them into one DingTalk message per instant_search run so we
   don't spam the channel.

The module is stateless apart from its Redis client access; safe to import
from every Celery task.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from app.core.config import get_settings


# ─────────────────────────────── 1. traffic shaping ──

async def human_delay(min_s: float = 1.2, max_s: float = 3.5) -> None:
    """Sleep a random duration in [min_s, max_s]. Default values are
    tuned to look like a distracted human reading a product card.
    """
    await asyncio.sleep(random.uniform(min_s, max_s))


async def scroll_like_human(page, total_scrolls: int = 3) -> None:
    """Scroll the page with non-uniform step sizes and random pauses.

    Real users don't scroll at a constant 2000px/step — they skim fast at
    first, then slow down as they decide whether to click. Randomise both
    the distance and the pause length to match that pattern.
    """
    for i in range(total_scrolls):
        distance = random.randint(400, 1800)
        try:
            await page.evaluate(f"window.scrollBy(0, {distance})")
        except Exception:
            return
        await human_delay(0.6, 1.4 + i * 0.3)


async def simulate_detail_dwell(page, max_clicks: int = 1) -> None:
    """Best-effort: click a random-looking card link to mimic "peek a
    detail then go back". All failures silently swallowed — this is
    decoration, not functionality.
    """
    try:
        links = await page.query_selector_all("a[href*='item'], a[href*='goods']")
        if not links:
            return
        target = random.choice(links[:20])
        async with page.context.expect_page(timeout=5000) as popup_info:
            await target.click(timeout=3000)
        popup = await popup_info.value
        await human_delay(3.0, 7.0)
        await popup.close()
    except Exception:
        pass


# ─────────────────────────────── 2. rate limiting ────

_RATE_PREFIX = "anti_risk:rate:"


async def rate_limit_guard(
    platform: str,
    max_per_hour: int | None = None,
    sleep_if_blocked: bool = False,
) -> bool:
    """Redis sliding-window counter. Returns True if the call is allowed.

    If ``sleep_if_blocked`` is True, blocks (up to 60s) waiting for the
    window to free up; otherwise returns False immediately so the caller
    can decide to skip or requeue.

    Counts one "call" per invocation; the caller is expected to invoke
    this before making a search request.
    """
    from app.services.proxy_service import _PerCallRedis

    settings = get_settings()
    limit = max_per_hour or settings.SELECTION_SEARCH_RATE_LIMIT_PER_HOUR
    if limit <= 0:
        return True

    window_key = f"{_RATE_PREFIX}{platform}:{int(time.time() // 3600)}"
    async with _PerCallRedis() as r:
        for _attempt in range(30 if sleep_if_blocked else 1):
            pipe = r.pipeline()
            pipe.incr(window_key)
            pipe.expire(window_key, 3700)
            count, _ = await pipe.execute()
            if int(count) <= limit:
                return True
            # Rolled back: we already incremented, now decrement to keep
            # the counter accurate for the next attempt's check.
            await r.decr(window_key)
            if not sleep_if_blocked:
                logger.warning(
                    f"anti_risk rate limit blocked: {platform} > {limit}/h"
                )
                return False
            logger.info(f"anti_risk rate limited for {platform}, waiting 2s")
            await asyncio.sleep(2.0)
    logger.warning(f"anti_risk rate wait timed out for {platform}")
    return False


# ─────────────────────────────── 3. risk signals ─────

# Patterns that indicate the platform suspects us. Split per-platform so
# false positives (e.g. xianyu has a real "登录" button even for guests)
# can be tuned independently.
_COMMON_RISK_PATTERNS: tuple[str, ...] = (
    "验证码",
    "异常访问",
    "访问异常",
    "操作太频繁",
    "系统繁忙",
    "滑动验证",
    "人机验证",
    "请重新登录",
    "账号被限",
    "账户已被限制",
    "您的访问触发了",
)

_PLATFORM_EXTRA: dict[str, tuple[str, ...]] = {
    "xianyu": ("RGV587", "无痕浏览"),
    "pdd": ("punish", "captcha"),
    "taobao": ("login.taobao.com", "punish", "baxia"),
    "1688": ("sec.1688.com", "_____tmd_____"),
    "xiaohongshu": ("please_login", "访问频次过高"),
}


@dataclass
class RiskSignal:
    platform: str
    signal_type: str  # e.g. "captcha_page", "login_redirect", "empty_result"
    detail: str = ""
    url: str | None = None
    captured_at: float = field(default_factory=time.time)


def scan_text_for_risk(
    platform: str, text: str | None,
) -> list[str]:
    """Return every risk-pattern match found in ``text`` for the platform.
    Empty list = nothing suspicious.
    """
    if not text:
        return []
    hits: list[str] = []
    patterns = _COMMON_RISK_PATTERNS + _PLATFORM_EXTRA.get(platform, ())
    lower = text.lower()
    for p in patterns:
        if p.lower() in lower:
            hits.append(p)
    return hits


async def detect_risk_in_page(platform: str, page) -> list[RiskSignal]:
    """Best-effort page-content scan for risk patterns. Never raises."""
    signals: list[RiskSignal] = []
    try:
        url = page.url or ""
    except Exception:
        url = ""
    url_hits = scan_text_for_risk(platform, url)
    for h in url_hits:
        signals.append(RiskSignal(
            platform=platform, signal_type="url_redirect",
            detail=h, url=url,
        ))
    try:
        body_sample = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 4000)"
        )
    except Exception:
        body_sample = ""
    text_hits = scan_text_for_risk(platform, body_sample)
    for h in text_hits:
        signals.append(RiskSignal(
            platform=platform, signal_type="risk_keyword",
            detail=h, url=url,
        ))
    return signals


# ─────────────────────────────── DingTalk aggregation ─

_ALERT_COOLDOWN_KEY = "anti_risk:alert_cooldown:"
_ALERT_COOLDOWN_SECONDS = 1800  # don't spam the same platform within 30min


async def flush_risk_alerts(
    keyword: str,
    signals_by_platform: dict[str, list[RiskSignal]],
) -> None:
    """Send one consolidated DingTalk notification for this run.

    Per-platform cooldown prevents repeat-pinging the same channel every
    60 seconds when a platform is down. ``signals_by_platform`` may
    contain empty lists — those are skipped silently.
    """
    from app.services.notification import notification_service
    from app.services.proxy_service import _PerCallRedis

    lines: list[str] = []
    platforms_to_cool: list[str] = []
    async with _PerCallRedis() as r:
        for platform, sigs in signals_by_platform.items():
            if not sigs:
                continue
            cooldown_key = f"{_ALERT_COOLDOWN_KEY}{platform}"
            if await r.get(cooldown_key):
                logger.info(
                    f"anti_risk alert suppressed for {platform} "
                    f"(still in cooldown)"
                )
                continue
            unique = {s.signal_type + "|" + s.detail for s in sigs}
            lines.append(
                f"- [{platform}] {len(sigs)} 次信号: "
                + ", ".join(sorted(unique))
            )
            platforms_to_cool.append(platform)
        if not lines:
            return
        for platform in platforms_to_cool:
            await r.set(
                f"{_ALERT_COOLDOWN_KEY}{platform}",
                "1",
                ex=_ALERT_COOLDOWN_SECONDS,
            )

    title = f"【风控预警】爬虫关键词 {keyword}"
    content = (
        f"爬虫在多个平台上命中风控信号，请检查：\n\n"
        + "\n".join(lines)
        + f"\n\n30 分钟内同平台不再重复告警。"
    )
    try:
        await notification_service.send_dingtalk(title=title, content=content)
        logger.warning(f"anti_risk alert sent for keyword '{keyword}'")
    except Exception as e:
        logger.error(f"anti_risk alert failed to send: {e}")


def risk_summary(signals: list[RiskSignal]) -> dict[str, Any]:
    """Compact JSON-friendly summary for storing into KeywordScore metadata."""
    if not signals:
        return {"count": 0}
    types: dict[str, int] = {}
    for s in signals:
        key = f"{s.signal_type}:{s.detail}"
        types[key] = types.get(key, 0) + 1
    return {
        "count": len(signals),
        "types": types,
        "first_url": signals[0].url,
    }
