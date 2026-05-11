"""
Per-account fingerprint management (V3 + V4).

Combines two anti-detection layers, both persisted into the existing
``Account.fingerprint`` JSON column so no schema migration is needed:

    1. Stable *hardware* fingerprints — every account has its own
       deterministic ``navigator.hardwareConcurrency`` / ``deviceMemory``
       / ``screen`` shape, sampled once from realistic distributions and
       reused forever. Without this, every account in the pool reports
       the same 8-core / 8-GB profile, which aggregates into an obvious
       "this is one machine" signal at the platform's risk-control side.

    2. Frozen *platform device cookies* — fingerprints like PDD's
       ``_nano_fp`` are computed client-side and rewritten each session.
       We capture them on the first successful crawl and replay them on
       every subsequent visit, so a single account appears as one
       *stable device* over months, even when its exit IP and timing
       vary.

Stored schema (``Account.fingerprint``)::

    {
        "version": 1,
        "hardware_concurrency": 6,
        "device_memory": 8,
        "screen": {"width": 1536, "height": 864, "color_depth": 24},
        "platform_str": "Win32",
        "frozen_cookies": {
            "pdd":   {"_nano_fp": "...", "api_uid": "..."},
            "1688":  {"cna": "...", "_tb_token_": "..."},
            "taobao":{"cookie2": "...", "_m_h5_tk": "..."}
        }
    }

Older accounts may have ``fingerprint=NULL`` or a partial dict;
``_ensure_shape`` backfills missing keys lazily on first access.
"""
from __future__ import annotations

import random
from typing import Any

from loguru import logger
from sqlalchemy import select, update

from app.core.database import AsyncSessionLocal
from app.models.system import Account


CURRENT_VERSION = 1

# Weighted hardware pools modelled on StatCounter / Chrome metrics for
# zh-CN desktop users (2025-Q4). Sum of weights must be 1.0.
_HW_CONCURRENCY_POOL: list[tuple[int, float]] = [
    (4, 0.20),
    (6, 0.30),
    (8, 0.30),
    (12, 0.15),
    (16, 0.05),
]
_DEVICE_MEMORY_POOL: list[tuple[int, float]] = [
    (4, 0.30),
    (8, 0.50),
    (16, 0.20),
]
_SCREEN_POOL: list[dict[str, int]] = [
    {"width": 1920, "height": 1080, "color_depth": 24},
    {"width": 1536, "height": 864, "color_depth": 24},
    {"width": 1366, "height": 768, "color_depth": 24},
    {"width": 1440, "height": 900, "color_depth": 24},
    {"width": 2560, "height": 1440, "color_depth": 24},
]
# navigator.platform — Win32 dominates zh-CN desktop traffic.
_PLATFORM_STR_POOL: list[str] = [
    "Win32", "Win32", "Win32", "Win32", "MacIntel", "Linux x86_64",
]

# Cookie names that act as *device-level* fingerprints per platform.
# We freeze these on a successful crawl and force-replay them on every
# subsequent session so the platform's risk model sees a stable device.
#
# Sources: PDD H5 inline JS (`_nano_fp` / `api_uid`), Alibaba/Taobao
# `cna` (UMID), `_tb_token_` is a CSRF cookie but its rotation rhythm
# is a fingerprint too.
FROZEN_COOKIE_KEYS: dict[str, set[str]] = {
    "pdd": {"_nano_fp", "api_uid", "_f77", "_a42", "pdd_user_uin"},
    "1688": {"cna", "_tb_token_", "xlly_s", "isg"},
    "taobao": {"cookie2", "_m_h5_tk", "_m_h5_tk_enc", "cna", "_tb_token_"},
}

# Default cookie domain hint when freezing → re-applying cookies for
# accounts that have never been seen by Playwright with this account.
_DOMAIN_HINTS: dict[str, str] = {
    "pdd": ".yangkeduo.com",
    "1688": ".1688.com",
    "taobao": ".taobao.com",
}


def _weighted_pick(pool: list[tuple[Any, float]]) -> Any:
    r = random.random()
    cum = 0.0
    for value, weight in pool:
        cum += weight
        if r < cum:
            return value
    return pool[-1][0]


def _generate_fingerprint() -> dict[str, Any]:
    """Pick a fresh, realistic hardware profile for a new account."""
    return {
        "version": CURRENT_VERSION,
        "hardware_concurrency": _weighted_pick(_HW_CONCURRENCY_POOL),
        "device_memory": _weighted_pick(_DEVICE_MEMORY_POOL),
        "screen": random.choice(_SCREEN_POOL),
        "platform_str": random.choice(_PLATFORM_STR_POOL),
        "frozen_cookies": {},
    }


def _ensure_shape(fp: dict[str, Any] | None) -> dict[str, Any]:
    """Backfill any missing keys on an older fingerprint dict.

    Treats the persisted value as authoritative for keys it already has
    (so we never reroll a stable hardware profile by accident). Only
    fills in keys absent from older records.
    """
    if not fp:
        return _generate_fingerprint()
    base = _generate_fingerprint()
    merged = {**base, **fp}
    merged["version"] = CURRENT_VERSION
    merged.setdefault("frozen_cookies", {})
    # Make sure nested dicts are real dicts (older JSONB rows have come
    # back as Decimal-flavoured lists in rare cases).
    if not isinstance(merged.get("screen"), dict):
        merged["screen"] = base["screen"]
    if not isinstance(merged.get("frozen_cookies"), dict):
        merged["frozen_cookies"] = {}
    return merged


async def get_or_init_fingerprint(account_id: str) -> dict[str, Any]:
    """Return the persisted fingerprint for ``account_id``.

    Generates and persists a fresh one on first access. Always returns a
    fully-shaped dict (no NULL handling needed at call sites).

    Idempotent: subsequent calls for the same account return the same
    hardware profile (frozen_cookies may grow as crawls succeed).
    """
    async with AsyncSessionLocal() as db:
        row = await db.execute(
            select(Account.fingerprint).where(Account.id == account_id)
        )
        existing = row.scalar_one_or_none()
        if (
            existing
            and isinstance(existing, dict)
            and existing.get("version") == CURRENT_VERSION
            and "hardware_concurrency" in existing
        ):
            existing.setdefault("frozen_cookies", {})
            return existing

        new_fp = _ensure_shape(existing if isinstance(existing, dict) else None)
        await db.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(fingerprint=new_fp)
        )
        await db.commit()
        logger.info(
            f"Fingerprint initialised for {account_id}: "
            f"hw={new_fp['hardware_concurrency']}, "
            f"mem={new_fp['device_memory']}, "
            f"screen={new_fp['screen']['width']}x{new_fp['screen']['height']}, "
            f"platform={new_fp['platform_str']}"
        )
        return new_fp


def generate_ephemeral_fingerprint() -> dict[str, Any]:
    """Hardware profile for one-off anonymous contexts (no account).

    Not persisted — used only to keep the stealth script's claimed
    hardware *internally consistent for one session* (instead of leaving
    the static 8/8 defaults that signal "another bot in the swarm").
    """
    return _generate_fingerprint()


async def freeze_platform_cookies(
    account_id: str,
    platform: str,
    cookies: list[dict[str, Any]],
) -> int:
    """Capture device-identity cookies after a successful crawl.

    Only the names in ``FROZEN_COOKIE_KEYS[platform]`` are persisted.
    Any cookie not seen this session keeps its previously-frozen value
    (we never *remove* a frozen entry by absence — sessions can drop
    cookies for many benign reasons).

    Returns the number of cookies frozen this call (0 if none of the
    target names appeared in the session jar, e.g. because the crawl
    failed before the platform JS ran).
    """
    keys = FROZEN_COOKIE_KEYS.get(platform)
    if not keys:
        return 0
    captured: dict[str, str] = {
        c["name"]: c["value"]
        for c in cookies
        if c.get("name") in keys and c.get("value")
    }
    if not captured:
        return 0

    async with AsyncSessionLocal() as db:
        row = await db.execute(
            select(Account.fingerprint).where(Account.id == account_id)
        )
        existing = row.scalar_one_or_none()
        fp = _ensure_shape(existing if isinstance(existing, dict) else None)
        platform_frozen = fp["frozen_cookies"].setdefault(platform, {})
        platform_frozen.update(captured)
        # SQLAlchemy's JSON change tracking is shallow; force a rebind
        # to the same dict so the UPDATE actually fires.
        fp["frozen_cookies"][platform] = dict(platform_frozen)
        await db.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(fingerprint=fp)
        )
        await db.commit()
    logger.info(
        f"Froze {len(captured)} {platform} cookies for {account_id}: "
        f"{sorted(captured.keys())}"
    )
    return len(captured)


def merge_frozen_into(
    fingerprint: dict[str, Any] | None,
    platform: str,
    cookies: list[dict[str, Any]] | None,
    domain_hint: str | None = None,
) -> list[dict[str, Any]]:
    """Return ``cookies`` with any frozen values for ``platform`` applied.

    Behaviour:
      * For each frozen ``(name, value)``: if a cookie with that name
        already exists in ``cookies``, its value is overwritten with the
        frozen one. Otherwise a new cookie is appended with the
        platform's default domain.
      * Non-frozen cookies are preserved verbatim.
      * Returns a new list — does not mutate ``cookies``.

    Call this in the crawler dispatcher (selection.py) before passing
    the cookie list to ``context.add_cookies``.
    """
    frozen = (fingerprint or {}).get("frozen_cookies", {}).get(platform, {})
    if not frozen:
        return list(cookies or [])
    domain = domain_hint or _DOMAIN_HINTS.get(platform, "")
    out: list[dict[str, Any]] = [dict(c) for c in (cookies or [])]
    by_name: dict[str, int] = {
        c.get("name", ""): i for i, c in enumerate(out) if c.get("name")
    }
    for name, value in frozen.items():
        if name in by_name:
            out[by_name[name]]["value"] = value
        elif domain:
            out.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
            })
    return out
