"""
Import PDD crawler cookies (Cookie-Editor export) into the ``accounts``
table under the ``pdd_crawler`` convention.

Why a separate script: the auto-login path (scripts/pdd_login.py) is
blocked by PDD's headless-chromium detection. Manual login + cookie
export is cheaper and lower-risk than maintaining a stealth-patched
headless setup.

Input format: either Playwright storage_state JSON (``{"cookies": [...], "origins": [...]}``)
or Cookie-Editor's bare array export (``[{...}, {...}]``). The script
normalises both into the storage_state shape that
``_load_crawler_cookies_sync`` expects.

Usage:
  python scripts/pdd_import_cookies.py \\
      --mobile 19955661876 \\
      --files cookies_yangkeduo.json cookies_pinduoduo.json
  # or pipe from stdin:
  cat cookies.json | python scripts/pdd_import_cookies.py \\
      --mobile 19955661876 --stdin
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _cookie_editor_to_playwright(raw: list[dict]) -> list[dict]:
    """Cookie-Editor's field names differ slightly from Playwright's.
    Convert in place so ``context.add_cookies()`` will accept them later.
    """
    out = []
    for c in raw:
        pw = {
            "name": c.get("name"),
            "value": c.get("value", ""),
            "domain": c.get("domain"),
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
        }
        # Cookie-Editor: sameSite in ("no_restriction","lax","strict"),
        # Playwright: ("None","Lax","Strict").
        ss_map = {
            "no_restriction": "None",
            "lax": "Lax",
            "strict": "Strict",
            "unspecified": "Lax",
        }
        ss = c.get("sameSite", "").lower()
        pw["sameSite"] = ss_map.get(ss, "Lax")
        exp = c.get("expirationDate") or c.get("expires")
        if exp and exp > 0:
            pw["expires"] = int(exp)
        if pw["name"] and pw["domain"]:
            out.append(pw)
    return out


def _normalise(payload) -> list[dict]:
    """Coerce whatever the user pasted into a flat list of Playwright-shaped
    cookie dicts."""
    if isinstance(payload, dict) and "cookies" in payload:
        # storage_state already
        return payload["cookies"]
    if isinstance(payload, list):
        # Cookie-Editor bare array
        return _cookie_editor_to_playwright(payload)
    raise ValueError(
        f"Unrecognised cookie payload shape: {type(payload).__name__}"
    )


async def _persist(mobile: str, cookies: list[dict]) -> None:
    import asyncpg
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.core.config import get_settings

    settings = get_settings()
    raw_url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    account_name = f"pdd_crawler_{mobile[-4:]}"
    storage_state = {"cookies": cookies, "origins": []}
    state_json = json.dumps(storage_state, ensure_ascii=False)

    conn = await asyncpg.connect(raw_url)
    try:
        existing = await conn.fetchrow(
            "SELECT id FROM accounts "
            "WHERE account_name = $1 AND platform = 'pdd_crawler'",
            account_name,
        )
        if existing:
            await conn.execute(
                "UPDATE accounts SET cookies_data = $1, is_active = true, "
                "session_status = 'active', updated_at = NOW() WHERE id = $2",
                state_json, existing["id"],
            )
            print(f"[ok] updated pdd_crawler '{account_name}' "
                  f"with {len(cookies)} cookies")
        else:
            await conn.execute(
                """
                INSERT INTO accounts
                    (id, account_name, platform, identity_group, lifecycle_stage,
                     daily_publish_limit, daily_published_count,
                     health_score, is_active, session_status,
                     cookies_data, created_at, updated_at)
                VALUES (gen_random_uuid(), $1, 'pdd_crawler', 'crawler', 'nurturing',
                        0, 0, 100, true, 'active',
                        $2, NOW(), NOW())
                """,
                account_name, state_json,
            )
            print(f"[ok] inserted pdd_crawler '{account_name}' "
                  f"with {len(cookies)} cookies")
    finally:
        await conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mobile", required=True)
    ap.add_argument("--files", nargs="*", default=[])
    ap.add_argument("--stdin", action="store_true")
    args = ap.parse_args()

    payloads: list = []
    for f in args.files:
        p = Path(f)
        if not p.exists():
            print(f"[err] file not found: {f}", file=sys.stderr)
            sys.exit(1)
        payloads.append(json.loads(p.read_text(encoding="utf-8")))
    if args.stdin:
        payloads.append(json.loads(sys.stdin.read()))
    if not payloads:
        ap.error("provide --files and/or --stdin")

    cookies: list[dict] = []
    seen_keys: set[tuple] = set()
    for pl in payloads:
        for c in _normalise(pl):
            k = (c.get("name"), c.get("domain"), c.get("path", "/"))
            if k in seen_keys:
                continue
            seen_keys.add(k)
            cookies.append(c)
    print(f"[info] merged {len(cookies)} unique cookies from "
          f"{len(payloads)} payload(s)")

    # Sanity check: PDD auth cookie must be present.
    auth_names = {
        "PDDAccessToken", "pdd_user_id", "pdd_user_uin",
        "PASS_ID", "api_uid",
    }
    found = sorted({c["name"] for c in cookies if c["name"] in auth_names})
    if not found:
        print(
            "[warn] no obvious PDD auth cookie (PDDAccessToken/pdd_user_id/PASS_ID) "
            "found — login may not stick. Continuing anyway.",
            file=sys.stderr,
        )
    else:
        print(f"[info] auth cookies present: {found}")

    asyncio.run(_persist(args.mobile, cookies))


if __name__ == "__main__":
    main()
