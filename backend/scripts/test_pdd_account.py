"""Health check for PDD crawler accounts.

Two modes:

1. Single-account diagnostic (verbose, with screenshot):
     python scripts/test_pdd_account.py --suffix 7315 --keyword 运动鞋

2. Full-pool batch check (table output, no screenshots):
     python scripts/test_pdd_account.py --all
     python scripts/test_pdd_account.py --all --disable-failed

Why this exists:
- New crawler accounts (just imported from cookies) need to be
  validated before joining the rotation. Nothing in the pool-rotation
  logic detects "the account loaded fine but PDD shadow-bans its
  searches" — we only see empty results and the号 accumulates burns.
- Periodic health checks catch slow decay (account being softly
  downgraded by PDD risk models over time).

Behavior on failure:
- By default, prints verdict but does NOT touch the DB. A single failed
  run could easily be a network blip, not an account problem.
- With ``--disable-failed``, accounts that return 0 items get
  ``is_active = false`` + a suspended_reason. They won't be picked
  again until manually reactivated.
- Cooldown / health_score are never touched by this script — those
  are for the real crawl path's feedback loop, not synthetic probes.

Each probe burns one compliance_gate slot (60s pacing + 5-25s jitter).
For a 5-account pool a full pass takes ~6-8 minutes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings


async def _load_named_account(
    suffix: str,
) -> tuple[str, str, list[dict], str | None] | None:
    raw_url = get_settings().DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    conn = await asyncpg.connect(raw_url)
    try:
        row = await conn.fetchrow(
            """
            SELECT id, account_name, cookies_data, bound_proxy_area
            FROM accounts
            WHERE platform = 'pdd_crawler'
              AND account_name LIKE $1
              AND is_active = true
              AND cookies_data IS NOT NULL
            LIMIT 1
            """,
            f"%{suffix}",
        )
        if not row:
            return None
        state = json.loads(row["cookies_data"])
        return (
            str(row["id"]),
            row["account_name"],
            state.get("cookies") or [],
            row["bound_proxy_area"],
        )
    finally:
        await conn.close()


async def _load_all_pdd_accounts(
    include_inactive: bool = False,
    include_cooldown: bool = False,
) -> list[tuple[str, str, list[dict], str | None, datetime | None]]:
    """Return [(id, name, cookies, area, cooldown_until), ...] for PDD pool."""
    raw_url = get_settings().DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    conn = await asyncpg.connect(raw_url)
    try:
        where = ["platform = 'pdd_crawler'", "cookies_data IS NOT NULL"]
        if not include_inactive:
            where.append("is_active = true")
        if not include_cooldown:
            where.append("(cooldown_until IS NULL OR cooldown_until <= now())")
        sql = f"""
            SELECT id, account_name, cookies_data, bound_proxy_area, cooldown_until
            FROM accounts
            WHERE {' AND '.join(where)}
            ORDER BY account_name
        """
        rows = await conn.fetch(sql)
        out = []
        for r in rows:
            try:
                state = json.loads(r["cookies_data"])
            except Exception:
                continue
            cookies = state.get("cookies") or []
            if not cookies:
                continue
            out.append((
                str(r["id"]), r["account_name"], cookies,
                r["bound_proxy_area"], r["cooldown_until"],
            ))
        return out
    finally:
        await conn.close()


async def _disable_account(account_id: str, reason: str) -> None:
    raw_url = get_settings().DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    conn = await asyncpg.connect(raw_url)
    try:
        await conn.execute(
            "UPDATE accounts SET is_active = false, suspended_reason = $2, "
            "updated_at = NOW() WHERE id = $1",
            account_id, reason[:500],
        )
    finally:
        await conn.close()


async def _probe_one(
    account: tuple[str, str, list[dict], str | None],
    keyword: str,
    proxy_url: str,
    *,
    verbose: bool = False,
) -> dict:
    """Run a single search probe for one account.

    We don't go through pdd_crawler.collect_market_data here — that
    function auto-closes the page, which means we can't inspect the
    final URL / body when items=0. Instead, run a minimal probe that
    mirrors the crawler's key steps but keeps the page alive until
    after we've collected diagnostics.

    Returns:
      {
        "account_id", "account_name", "area",
        "items" (int), "error" (str|None),
        "final_url" (str|None), "body_preview" (str|None),
        "screenshot" (str|None),
        "verdict": "pass"|"fail"|"error",
      }
    """
    from app.services.browser import browser_manager
    from app.services.human_behavior import humanize_page

    aid, aname, cookies, area = account
    out: dict = {
        "account_id": aid, "account_name": aname, "area": area,
        "items": 0, "error": None, "final_url": None,
        "body_preview": None, "screenshot": None,
        "verdict": "fail", "risks": [],
    }

    try:
        ctx = await browser_manager.get_anonymous_context(
            proxy_url=proxy_url, platform="pdd",
            proxy_area=area, mobile=True,
        )
    except Exception as e:
        out["error"] = f"ctx_create: {e!r}"
        out["verdict"] = "error"
        return out

    page = None
    try:
        if cookies:
            await ctx.add_cookies(cookies)
        page = await ctx.new_page()

        from urllib.parse import quote
        url = (f"https://mobile.yangkeduo.com/search_result.html"
               f"?search_key={quote(keyword)}&source=index")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        except Exception as e:
            out["error"] = f"goto: {e!r}"
            out["risks"].append("goto_failed")

        try:
            await humanize_page(page, scroll_px=400)
        except Exception:
            pass

        # Read PDD's SSR list (same JS the real crawler uses).
        js_read = (
            "() => (window.rawData && window.rawData.stores "
            "&& window.rawData.stores.store && window.rawData.stores.store.data "
            "&& window.rawData.stores.store.data.ssrListData "
            "&& window.rawData.stores.store.data.ssrListData.list) || []"
        )
        raw_list: list = []
        for _ in range(6):
            try:
                raw_list = await page.evaluate(js_read) or []
            except Exception as e:
                out["risks"].append(f"eval_fail:{type(e).__name__}")
                raw_list = []
            if raw_list:
                break
            try:
                await page.evaluate("window.scrollBy(0, 800)")
            except Exception:
                pass
            await asyncio.sleep(1.2)

        out["items"] = len(raw_list)

        try:
            out["final_url"] = page.url
        except Exception:
            pass
        try:
            preview = await page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 300)"
            )
            out["body_preview"] = (preview or "").strip()
        except Exception:
            pass

        if verbose and not raw_list:
            shot = f"/tmp/pdd_health_{aname}.png"
            try:
                await page.screenshot(path=shot, full_page=False)
                out["screenshot"] = shot
            except Exception:
                pass

        out["verdict"] = "pass" if raw_list else "fail"
    except Exception as e:
        out["error"] = f"probe: {e!r}"
        out["verdict"] = "error"
    finally:
        try:
            if page and not page.is_closed():
                await page.close()
        except Exception:
            pass
        try:
            await ctx.close()
        except Exception:
            pass
    return out


def _print_row(r: dict) -> None:
    verdict_icon = {"pass": "✓", "fail": "✗", "error": "!"}[r["verdict"]]
    tag = r["verdict"].upper()
    items = str(r["items"])
    risks = ",".join(r["risks"]) or "-"
    err = r.get("error") or ""
    url = r.get("final_url") or "-"
    url_short = url if len(url) < 55 else url[:52] + "..."
    print(
        f"  {verdict_icon} {tag:<6} {r['account_name']:<22} "
        f"area={str(r['area'] or '-'):<7} items={items:<4} "
        f"risks={risks:<18} url={url_short}"
    )
    if err:
        print(f"      error: {err}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suffix", help="probe one account by name suffix, e.g. 7315")
    ap.add_argument("--all", action="store_true",
                    help="probe every active PDD crawler account not in cooldown")
    ap.add_argument("--keyword", default="运动鞋",
                    help="search keyword (default: 运动鞋)")
    ap.add_argument("--proxy-area", default=None,
                    help="override bound_proxy_area for this run only "
                         "(single-account mode)")
    ap.add_argument("--disable-failed", action="store_true",
                    help="in --all mode, deactivate accounts that return 0 items")
    ap.add_argument("--include-cooldown", action="store_true",
                    help="in --all mode, also probe accounts currently in cooldown")
    args = ap.parse_args()

    if not args.suffix and not args.all:
        ap.error("specify --suffix <xxxx> OR --all")

    proxy_url = getattr(get_settings(), "SELECTION_CRAWLER_PROXY_URL", None)
    print(f"[info] proxy_url={proxy_url}")
    print(f"[info] keyword={args.keyword!r}")
    print(f"[info] utc_now={datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}")

    if args.suffix:
        acc = await _load_named_account(args.suffix)
        if not acc:
            print(f"[err] no active PDD account matching *{args.suffix}")
            sys.exit(2)
        aid, aname, cookies, area = acc
        area = args.proxy_area or area
        print(f"\n[info] probing {aname} ({aid}), "
              f"{len(cookies)} cookies, area={area or '(unbound)'}")
        r = await _probe_one((aid, aname, cookies, area), args.keyword,
                             proxy_url, verbose=True)
        print("\n" + "=" * 70)
        _print_row(r)
        if r.get("screenshot"):
            print(f"      screenshot: {r['screenshot']}")
        print("=" * 70)
        sys.exit(0 if r["verdict"] == "pass" else 1)

    # --all mode
    accounts = await _load_all_pdd_accounts(
        include_cooldown=args.include_cooldown
    )
    if not accounts:
        print("[err] no eligible PDD accounts in the pool "
              "(all inactive or in cooldown)")
        sys.exit(2)

    print(f"\n[info] probing {len(accounts)} account(s). "
          f"Each probe ~60-90s; total ~{len(accounts) * 75}s.")
    results: list[dict] = []
    for i, acc in enumerate(accounts, 1):
        aid, aname, cookies, area, cooldown = acc
        cd_note = ""
        if cooldown and cooldown > datetime.now(timezone.utc):
            h = (cooldown - datetime.now(timezone.utc)).total_seconds() / 3600
            cd_note = f" [IN COOLDOWN +{h:.1f}h]"
        print(f"\n[{i}/{len(accounts)}] {aname} "
              f"(area={area or '-'}){cd_note}")
        r = await _probe_one((aid, aname, cookies, area),
                             args.keyword, proxy_url, verbose=False)
        _print_row(r)
        results.append(r)

    # Summary
    n_pass = sum(1 for r in results if r["verdict"] == "pass")
    n_fail = sum(1 for r in results if r["verdict"] == "fail")
    n_err = sum(1 for r in results if r["verdict"] == "error")
    print("\n" + "=" * 70)
    print(f"Summary: {n_pass} pass · {n_fail} fail · {n_err} error"
          f" (of {len(results)} total)")

    # Optional: disable persistent failures.
    if args.disable_failed and n_fail > 0:
        to_disable = [r for r in results if r["verdict"] == "fail"]
        print(f"\n[action] --disable-failed: deactivating {len(to_disable)} "
              f"account(s)")
        for r in to_disable:
            reason = (
                f"health_check: 0 items for '{args.keyword}'; "
                f"final_url={r.get('final_url') or 'n/a'}"
            )
            await _disable_account(r["account_id"], reason)
            print(f"  ✗ {r['account_name']} → is_active=false")

    sys.exit(0 if n_fail == 0 and n_err == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
