"""Live (in-cluster) verification of V3 + V4 wiring.

Designed to be run inside the backend pod so it can talk to Playwright,
Redis, Postgres, and the proxy resolver exactly like the crawler tasks do.

Checks:
  V3-A: per-account stable hw fingerprint is read from DB and injected
        into the stealth script. Open a context with ``account_id=X``,
        navigate to ``about:blank``, and read ``navigator.hardwareConcurrency``
        / ``deviceMemory`` / ``screen.*`` / ``navigator.platform`` from
        the live page — they must match the DB-persisted values.

  V3-B: anonymous contexts (no account_id) get *some* hw value (not the
        stale 8/8 default everywhere) — we just assert it's set.

  V4-A: ``merge_frozen_into`` would override stored cookies if we had
        anything frozen. We seed a fake frozen value in fingerprint,
        merge, and assert.

  V4-B: ``freeze_platform_cookies`` writes back to the DB. We seed a
        fake cookie list, call freeze, re-read, and assert.

Run inside the pod::

    kubectl -n ns-3zn44u6p exec deploy/backend -- python3 scripts/test_v3_v4_live.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.account_fingerprint import (  # noqa: E402
    freeze_platform_cookies,
    get_or_init_fingerprint,
    merge_frozen_into,
)


async def _v3_account_stable(account_name: str = "pdd_crawler_0043") -> None:
    print(f"\nV3-A: per-account stable fingerprint ({account_name})")
    # Resolve account_id via the DB
    import asyncpg
    from app.core.config import get_settings
    raw = get_settings().DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    conn = await asyncpg.connect(raw)
    try:
        row = await conn.fetchrow(
            "SELECT id, fingerprint FROM accounts WHERE account_name = $1",
            account_name,
        )
    finally:
        await conn.close()
    if not row:
        print(f"   SKIP — account {account_name} not found")
        return
    account_id = str(row["id"])
    fp_db = await get_or_init_fingerprint(account_id)
    expect_hw = fp_db["hardware_concurrency"]
    expect_mem = fp_db["device_memory"]
    expect_w = fp_db["screen"]["width"]
    expect_h = fp_db["screen"]["height"]
    expect_plat = fp_db["platform_str"]
    print(
        f"   DB says: hw={expect_hw}, mem={expect_mem}, "
        f"screen={expect_w}x{expect_h}, plat={expect_plat}"
    )

    from app.services.browser import browser_manager
    ctx = await browser_manager.get_anonymous_context(
        proxy_url=None, platform="pdd",
        account_id=account_id, mobile=False,
    )
    page = await ctx.new_page()
    try:
        await page.goto("about:blank")
        observed = await page.evaluate("""() => ({
            hw: navigator.hardwareConcurrency,
            mem: navigator.deviceMemory,
            sw: window.screen.width,
            sh: window.screen.height,
            plat: navigator.platform,
            webdriver: navigator.webdriver,
            languages: navigator.languages,
        })""")
    finally:
        await page.close()

    print(f"   browser says: {observed}")
    issues = []
    if observed["hw"] != expect_hw:
        issues.append(f"hw {observed['hw']} != {expect_hw}")
    if observed["mem"] != expect_mem:
        issues.append(f"mem {observed['mem']} != {expect_mem}")
    if observed["sw"] != expect_w:
        issues.append(f"sw {observed['sw']} != {expect_w}")
    if observed["sh"] != expect_h:
        issues.append(f"sh {observed['sh']} != {expect_h}")
    if observed["plat"] != expect_plat:
        issues.append(f"platform {observed['plat']!r} != {expect_plat!r}")
    if observed["webdriver"] not in (None, False):
        issues.append(f"webdriver leak: {observed['webdriver']!r}")
    if not observed["languages"]:
        issues.append(f"languages empty: {observed['languages']!r}")
    if issues:
        print("   FAIL — " + "; ".join(issues))
        return False
    print("   PASS")
    return True


async def _v3_anonymous_consistent() -> None:
    print("\nV3-B: anonymous context still gets a hw profile injected")
    from app.services.browser import browser_manager
    ctx = await browser_manager.get_anonymous_context(
        proxy_url=None, platform="pdd", mobile=False,
    )
    page = await ctx.new_page()
    try:
        await page.goto("about:blank")
        observed = await page.evaluate("""() => ({
            hw: navigator.hardwareConcurrency,
            mem: navigator.deviceMemory,
            plat: navigator.platform,
        })""")
    finally:
        await page.close()
    print(f"   observed: {observed}")
    if observed["hw"] and observed["mem"] and observed["plat"]:
        print("   PASS")
        return True
    print("   FAIL — anonymous context did not get a fingerprint")
    return False


async def _v4_merge_and_freeze(account_name: str = "pdd_crawler_0043") -> bool:
    print(f"\nV4: freeze + merge round-trip ({account_name})")
    import asyncpg
    from app.core.config import get_settings
    raw = get_settings().DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    conn = await asyncpg.connect(raw)
    try:
        row = await conn.fetchrow(
            "SELECT id FROM accounts WHERE account_name = $1",
            account_name,
        )
    finally:
        await conn.close()
    if not row:
        print(f"   SKIP — account {account_name} not found")
        return None
    account_id = str(row["id"])

    # Freeze a synthetic cookie set, then re-read and assert.
    synth = [
        {"name": "_nano_fp", "value": "TESTFP_LIVE_V4", "domain": ".yangkeduo.com"},
        {"name": "api_uid",  "value": "TESTAUX",        "domain": ".yangkeduo.com"},
        {"name": "unrelated", "value": "X",             "domain": ".yangkeduo.com"},
    ]
    n = await freeze_platform_cookies(account_id, "pdd", synth)
    print(f"   freeze_platform_cookies wrote {n} cookies")

    fp = await get_or_init_fingerprint(account_id)
    frozen = fp.get("frozen_cookies", {}).get("pdd", {})
    print(f"   DB now reports frozen pdd cookies: {sorted(frozen.keys())}")

    if frozen.get("_nano_fp") != "TESTFP_LIVE_V4":
        print(f"   FAIL — _nano_fp not persisted (got {frozen.get('_nano_fp')!r})")
        return False
    if "unrelated" in frozen:
        print("   FAIL — non-frozen cookie leaked into frozen_cookies")
        return False

    # Merge: simulate what selection.py does pre-crawl
    stored = [
        {"name": "_nano_fp", "value": "STALE", "domain": ".yangkeduo.com", "path": "/"},
        {"name": "session", "value": "abc", "domain": ".yangkeduo.com", "path": "/"},
    ]
    merged = merge_frozen_into(fp, "pdd", stored, ".yangkeduo.com")
    by_name = {c["name"]: c["value"] for c in merged}
    if by_name["_nano_fp"] != "TESTFP_LIVE_V4":
        print(f"   FAIL — merge did not override _nano_fp (got {by_name['_nano_fp']!r})")
        return False
    if by_name["session"] != "abc":
        print(f"   FAIL — merge corrupted non-frozen cookie")
        return False
    if "api_uid" not in by_name:
        print("   FAIL — merge did not append missing frozen cookie")
        return False
    print(f"   merge produced {len(merged)} cookies with override applied")
    print("   PASS")
    return True


async def _cleanup_test_freeze(account_name: str = "pdd_crawler_0043") -> None:
    """Remove the synthetic ``TESTFP_LIVE_V4`` from the account so it
    doesn't interfere with the real crawler later. We re-freeze with an
    empty placeholder-only call would do nothing (the API never deletes),
    so we touch the DB directly."""
    import asyncpg
    import json
    from app.core.config import get_settings
    raw = get_settings().DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    conn = await asyncpg.connect(raw)
    try:
        row = await conn.fetchrow(
            "SELECT fingerprint FROM accounts WHERE account_name = $1",
            account_name,
        )
        fp = row["fingerprint"] if row else None
        if isinstance(fp, str):
            fp = json.loads(fp)
        if not fp:
            return
        platform_frozen = fp.get("frozen_cookies", {}).get("pdd", {})
        cleaned = {
            k: v for k, v in platform_frozen.items()
            if v not in ("TESTFP_LIVE_V4", "TESTAUX")
        }
        fp["frozen_cookies"]["pdd"] = cleaned
        await conn.execute(
            "UPDATE accounts SET fingerprint = $1::jsonb WHERE account_name = $2",
            json.dumps(fp), account_name,
        )
        print(f"\n   (cleanup) removed synthetic frozen cookies from {account_name}")
    finally:
        await conn.close()


async def _main() -> int:
    results = []
    results.append(await _v3_account_stable("pdd_crawler_0043"))
    results.append(await _v3_anonymous_consistent())
    results.append(await _v4_merge_and_freeze("pdd_crawler_0043"))
    await _cleanup_test_freeze("pdd_crawler_0043")

    # Tear down playwright cleanly
    try:
        from app.services.browser import browser_manager
        if browser_manager._browser:
            await browser_manager.stop()
    except Exception:
        pass

    print("\n" + "=" * 60)
    passed = sum(1 for r in results if r is True)
    skipped = sum(1 for r in results if r is None)
    total = len(results)
    print(f"V3/V4 live: {passed}/{total - skipped} passed"
          + (f", {skipped} skipped" if skipped else ""))
    return 0 if passed == total - skipped else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
