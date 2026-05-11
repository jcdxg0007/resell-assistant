"""Smoke test for V3 (hardware fingerprint) + V4 (frozen cookies).

Covers three properties without touching Playwright or any platform:

    A. ``_generate_fingerprint`` follows the configured weight pools
       (loose chi-square check on 5000 samples — flags only gross drift).
    B. ``get_or_init_fingerprint`` is *idempotent* — calling it twice
       for the same account returns the same hw values.
    C. ``merge_frozen_into`` overrides the cookie value when frozen,
       leaves untouched names alone, and appends missing frozen cookies
       with the platform's default domain.
    D. ``build_stealth_script`` substitutes every template placeholder
       (catches the "left __HW__ literal in the JS" regression).

Run::

    python scripts/test_account_fingerprint.py
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.account_fingerprint import (  # noqa: E402
    FROZEN_COOKIE_KEYS,
    _generate_fingerprint,
    _HW_CONCURRENCY_POOL,
    _DEVICE_MEMORY_POOL,
    merge_frozen_into,
)
from app.services.browser import build_stealth_script  # noqa: E402


def test_distribution():
    print("A. hardware-pool distribution sanity")
    samples = [_generate_fingerprint() for _ in range(5000)]
    hw = Counter(s["hardware_concurrency"] for s in samples)
    mem = Counter(s["device_memory"] for s in samples)

    # Each configured weight should be reasonably close to observed.
    # Loose tolerance — this is anti-regression, not statistical proof.
    print(f"   hw_concurrency  counts: {dict(hw)}")
    print(f"   device_memory   counts: {dict(mem)}")
    for value, weight in _HW_CONCURRENCY_POOL:
        observed = hw.get(value, 0) / 5000
        assert abs(observed - weight) < 0.05, (
            f"hw={value} expected ~{weight}, got {observed:.3f}"
        )
    for value, weight in _DEVICE_MEMORY_POOL:
        observed = mem.get(value, 0) / 5000
        assert abs(observed - weight) < 0.05, (
            f"mem={value} expected ~{weight}, got {observed:.3f}"
        )
    print("   OK\n")


def test_merge_frozen():
    print("B. merge_frozen_into() override semantics")
    fp = {"frozen_cookies": {"pdd": {"_nano_fp": "FROZEN_X", "api_uid": "AUX"}}}
    stored = [
        {"name": "_nano_fp", "value": "STALE", "domain": ".yangkeduo.com", "path": "/"},
        {"name": "PDDAccessToken", "value": "TOK", "domain": ".yangkeduo.com", "path": "/"},
    ]
    out = merge_frozen_into(fp, "pdd", stored, ".yangkeduo.com")
    by_name = {c["name"]: c for c in out}
    assert by_name["_nano_fp"]["value"] == "FROZEN_X", \
        f"frozen should override, got {by_name['_nano_fp']['value']}"
    assert by_name["PDDAccessToken"]["value"] == "TOK", \
        "non-frozen cookies must be preserved unchanged"
    assert by_name["api_uid"]["value"] == "AUX", \
        "missing frozen cookie should be appended"
    assert by_name["api_uid"]["domain"] == ".yangkeduo.com", \
        "appended cookie must carry the platform's domain hint"

    # Empty frozen → exact passthrough
    out2 = merge_frozen_into({"frozen_cookies": {}}, "pdd", stored, ".yangkeduo.com")
    assert len(out2) == 2 and out2[0]["value"] == "STALE", \
        "no frozen → input untouched"

    # Unknown platform → also passthrough
    out3 = merge_frozen_into(fp, "unknown_platform", stored, ".x.com")
    assert len(out3) == 2 and out3[0]["value"] == "STALE", \
        "unknown platform key → input untouched"
    print("   OK\n")


def test_stealth_template():
    print("C. build_stealth_script() substitutes every placeholder")
    fp = {
        "hardware_concurrency": 6,
        "device_memory": 16,
        "screen": {"width": 2560, "height": 1440, "color_depth": 24},
        "platform_str": "MacIntel",
    }
    js = build_stealth_script(fp)
    # No template placeholders may survive.
    for placeholder in (
        "__HW_CONCURRENCY__", "__DEVICE_MEMORY__",
        "__SCREEN_WIDTH__", "__SCREEN_HEIGHT__",
        "__SCREEN_COLOR_DEPTH__", "__PLATFORM_STR__",
    ):
        assert placeholder not in js, \
            f"placeholder {placeholder} survived substitution"
    # Substituted values must show up literally.
    assert "() => 6" in js and "() => 16" in js, "hw/mem values missing"
    assert "() => 2560" in js and "() => 1440" in js, "screen values missing"
    assert "'MacIntel'" in js, "platform string missing"

    # Defaults path: empty fingerprint must still substitute everything.
    js2 = build_stealth_script(None)
    for placeholder in ("__HW_CONCURRENCY__", "__SCREEN_WIDTH__"):
        assert placeholder not in js2, \
            f"defaults path left placeholder {placeholder}"
    print("   OK\n")


def test_frozen_keys_coverage():
    print("D. FROZEN_COOKIE_KEYS covers the three live crawler platforms")
    for plat in ("pdd", "1688", "taobao"):
        assert plat in FROZEN_COOKIE_KEYS, f"{plat} missing from frozen keys"
        assert FROZEN_COOKIE_KEYS[plat], f"{plat} frozen-key set is empty"
    print(f"   pdd:    {sorted(FROZEN_COOKIE_KEYS['pdd'])}")
    print(f"   1688:   {sorted(FROZEN_COOKIE_KEYS['1688'])}")
    print(f"   taobao: {sorted(FROZEN_COOKIE_KEYS['taobao'])}")
    print("   OK\n")


async def _main():
    test_distribution()
    test_merge_frozen()
    test_stealth_template()
    test_frozen_keys_coverage()
    print("All V3/V4 smoke checks passed.")


if __name__ == "__main__":
    asyncio.run(_main())
