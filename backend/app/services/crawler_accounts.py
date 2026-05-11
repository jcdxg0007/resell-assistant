"""
Crawler-pool rotation for PDD / 1688 / (later) XHS 小号.

Problem: 2026 PDD/1688 flag any account that issues more than ~8-12
searches in 30 minutes — the platform serves a zero-width-unicode
"ghost page" so scraping still 200s but returns no items. Detecting
this is easy (see the ``empty_result`` RiskSignal). Recovering is not:
the account stays burnt for ~6-24 hours.

The fix is to keep N (≥3) crawler小号 per platform and rotate them.
This module encapsulates the rotation policy:

    pick_crawler_account(platform)  -- grabs the least-recently-used
                                       account that isn't in cooldown
    mark_crawler_success(account_id) -- bumps last_used_at
    mark_crawler_burnt(account_id)   -- sets cooldown_until = now + 60m

Design choices:
- We write directly with asyncpg inside a fresh loop so this can be
  called both from sync Celery tasks (via ``_sync``) and from the
  existing async ``_instant_search`` flow without an AsyncSession
  bleed-through (the orchestrator already spawns its own asyncpg
  connection for similar reasons — see _load_account_config_sync).
- cookies_data is returned alongside the id so the caller doesn't need
  a second round-trip. The orchestrator treats this tuple as opaque
  — it only needs the cookies for injection and the id to report back.
"""
from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta, timezone

import asyncpg
from loguru import logger

from app.core.config import get_settings


# After a burn event the account is off-rotation for this long. PDD's
# real shadow-ban often lasts longer (up to a day), but 60 minutes is
# the right length for *our* cooldown — it prevents us from hammering a
# flagged account further. If the 60m re-try also comes back empty, the
# account gets bumped again automatically (next mark_crawler_burnt).
COOLDOWN_MINUTES = 60


# Geographic home-town pool for crawler accounts. Each new crawler account
# is assigned one of these areas on first pick and never moves — so a号
# is "a Fuzhou user" for life, not "a user who bounces between 5 cities".
# Values are GB-T 2260 province-level codes (青果 accepts both 2-digit
# province and 6-digit city; here we use provincial codes so each account
# has a consistent "home province" while the per-area IP pool still
# rotates city-level IPs naturally).
#
# Picked: economically active provinces with broad residential IP coverage
# on 青果's pool, so we avoid IPs that "don't match the user profile"
# (e.g. a 新疆 IP for a Taobao shopper looks unusual).
DEFAULT_ACCOUNT_AREAS: tuple[str, ...] = (
    "350000",  # 福建
    "330000",  # 浙江
    "320000",  # 江苏
    "440000",  # 广东
    "510000",  # 四川
    "420000",  # 湖北
    "370000",  # 山东
    "410000",  # 河南
)


def _raw_db_url() -> str:
    return get_settings().DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://"
    )


async def _claim_account(conn: asyncpg.Connection, platform: str) -> asyncpg.Record | None:
    """Atomically pick the least-recently-used crawler account for
    ``platform`` that isn't currently in cooldown.

    We combine SELECT ... FOR UPDATE SKIP LOCKED with an in-query
    ``last_used_at = now()`` update so two concurrent callers can't
    grab the same account. ``cookies_data`` and ``bound_proxy_area``
    are returned alongside, so the caller doesn't round-trip again.

    If the picked account has no ``bound_proxy_area`` yet (fresh号
    or pre-area-feature legacy), we also assign one inside the same
    transaction, picking the least-used area in the pool — that keeps
    the area distribution balanced instead of repeatedly homing new
    accounts to whichever area happened to be first in the list.
    """
    row = await conn.fetchrow(
        """
        WITH candidate AS (
            SELECT id
            FROM accounts
            WHERE platform = $1
              AND is_active = true
              AND session_status IN ('active', 'none')
              AND cookies_data IS NOT NULL
              AND (cooldown_until IS NULL OR cooldown_until <= now())
            ORDER BY last_used_at NULLS FIRST, health_score DESC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        UPDATE accounts
        SET last_used_at = now()
        WHERE id = (SELECT id FROM candidate)
        RETURNING id, account_name, cookies_data, health_score,
                  last_used_at, cooldown_until, bound_proxy_area, platform
        """,
        platform,
    )
    return row


async def _assign_area_if_missing(
    conn: asyncpg.Connection, account_id: str, platform: str
) -> str:
    """Pick the least-populated area in DEFAULT_ACCOUNT_AREAS and bind
    it to this account. Called only when bound_proxy_area is NULL.

    Rationale for "least-populated": when the user gradually grows
    the号 pool, we want areas to fill up evenly. If we instead used
    ``random.choice`` the first 3 accounts could all land in the same
    area by chance, defeating the "look like N users from different
    cities" goal.
    """
    rows = await conn.fetch(
        """
        SELECT bound_proxy_area, count(*) AS n
        FROM accounts
        WHERE platform = $1 AND bound_proxy_area IS NOT NULL
        GROUP BY bound_proxy_area
        """,
        platform,
    )
    used: dict[str, int] = {r["bound_proxy_area"]: r["n"] for r in rows}
    # Among the configured pool, pick whichever has the lowest count.
    # Ties broken randomly so restart ordering doesn't always pick the
    # same first-match.
    counts = [(a, used.get(a, 0)) for a in DEFAULT_ACCOUNT_AREAS]
    min_count = min(c for _, c in counts)
    candidates = [a for a, c in counts if c == min_count]
    area = random.choice(candidates)
    await conn.execute(
        "UPDATE accounts SET bound_proxy_area = $2 WHERE id = $1",
        account_id, area,
    )
    logger.info(
        f"Auto-assigned bound_proxy_area={area} to {platform} account "
        f"{account_id} (pool distribution after: "
        f"{dict([(a, used.get(a, 0) + (1 if a == area else 0)) for a in DEFAULT_ACCOUNT_AREAS])})"
    )
    return area


async def _pick_async(
    platform_tag: str,
) -> tuple[str, str, list[dict], str | None] | None:
    """Return (account_id, account_name, cookies, bound_area) or None
    if the pool is empty / all in cooldown.
    """
    platform = f"{platform_tag}_crawler"
    conn = await asyncpg.connect(_raw_db_url())
    try:
        async with conn.transaction():
            row = await _claim_account(conn, platform)
            if not row:
                return None
            area = row["bound_proxy_area"]
            if not area:
                area = await _assign_area_if_missing(
                    conn, str(row["id"]), platform
                )

        try:
            state = json.loads(row["cookies_data"])
        except Exception as e:
            logger.warning(
                f"[{platform}] account {row['account_name']} has malformed "
                f"cookies_data: {e}"
            )
            return None

        cookies = state.get("cookies") or []
        if not cookies:
            return None

        return str(row["id"]), row["account_name"], cookies, area
    finally:
        await conn.close()


async def _report_async(
    account_id: str,
    *,
    burnt: bool,
    reason: str | None = None,
) -> None:
    """Write back the crawling outcome.

    - ``burnt=False`` → just bumps ``last_used_at`` (already done in
      _claim_account, so this is cheap/no-op but kept for symmetry and
      for future success-signal accounting).
    - ``burnt=True`` → sets ``cooldown_until = now + 60m`` and records
      ``suspended_reason`` so operators can see why in the admin UI.
    """
    conn = await asyncpg.connect(_raw_db_url())
    try:
        if burnt:
            until = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
            await conn.execute(
                """
                UPDATE accounts
                SET cooldown_until = $2,
                    suspended_reason = $3,
                    health_score = GREATEST(0, health_score - 5)
                WHERE id = $1
                """,
                account_id, until, (reason or "crawler_empty_result")[:500],
            )
            logger.warning(
                f"Crawler account {account_id} quarantined until {until:%H:%M:%S} "
                f"— reason: {reason or 'empty_result'}"
            )
        else:
            await conn.execute(
                """
                UPDATE accounts
                SET health_score = LEAST(100, health_score + 1)
                WHERE id = $1
                """,
                account_id,
            )
    finally:
        await conn.close()


# ─── sync wrappers ────────────────────────────────────────────────────
#
# Celery tasks are sync (Celery's execution model) but most of our DB
# code is async. We wrap with a fresh event loop the same way
# ``_load_crawler_cookies_sync`` already does. The async callers just
# use the *_async variants directly.

def pick_crawler_account_sync(
    platform_tag: str,
) -> tuple[str, str, list[dict], str | None] | None:
    """Sync wrapper returning (id, name, cookies, bound_area) or None."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_pick_async(platform_tag))
    except Exception as e:
        logger.warning(f"pick_crawler_account failed for {platform_tag}: {e}")
        return None
    finally:
        loop.close()


def report_crawler_result_sync(
    account_id: str | None,
    *,
    burnt: bool,
    reason: str | None = None,
) -> None:
    if not account_id:
        return
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            _report_async(account_id, burnt=burnt, reason=reason)
        )
    except Exception as e:
        logger.warning(f"report_crawler_result failed for {account_id}: {e}")
    finally:
        loop.close()


async def report_crawler_result(
    account_id: str | None,
    *,
    burnt: bool,
    reason: str | None = None,
) -> None:
    """Async variant — use inside _instant_search which already runs
    under an asyncio loop."""
    if not account_id:
        return
    try:
        await _report_async(account_id, burnt=burnt, reason=reason)
    except Exception as e:
        logger.warning(f"report_crawler_result failed for {account_id}: {e}")
