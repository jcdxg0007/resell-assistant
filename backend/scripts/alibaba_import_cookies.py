"""
Import 1688 crawler cookies (Cookie-Editor export) into the ``accounts``
table under the ``1688_crawler`` convention.

Mirrors ``pdd_import_cookies.py`` but targets the Alibaba ecosystem:
login state is split across ``.1688.com`` / ``.alibaba.com`` /
``.taobao.com`` / ``login.1688.com``. The user pastes one JSON export
per domain; we merge + dedup before persisting.

Usage:
  python scripts/alibaba_import_cookies.py \\
      --mobile 1234567890 \\
      --files 1688.json alibaba.json taobao.json login_1688.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _cookie_editor_to_playwright(raw: list[dict]) -> list[dict]:
    """Cookie-Editor → Playwright shape (matches pdd_import_cookies)."""
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
    if isinstance(payload, dict) and "cookies" in payload:
        return payload["cookies"]
    if isinstance(payload, list):
        return _cookie_editor_to_playwright(payload)
    raise ValueError(f"Unrecognised payload: {type(payload).__name__}")


async def _persist(mobile: str, cookies: list[dict]) -> None:
    import asyncpg
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.core.config import get_settings

    settings = get_settings()
    raw_url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    account_name = f"1688_crawler_{mobile[-4:]}"
    storage_state = {"cookies": cookies, "origins": []}
    state_json = json.dumps(storage_state, ensure_ascii=False)

    conn = await asyncpg.connect(raw_url)
    try:
        existing = await conn.fetchrow(
            "SELECT id FROM accounts "
            "WHERE account_name = $1 AND platform = '1688_crawler'",
            account_name,
        )
        if existing:
            await conn.execute(
                "UPDATE accounts SET cookies_data = $1, is_active = true, "
                "session_status = 'active', updated_at = NOW() WHERE id = $2",
                state_json, existing["id"],
            )
            print(f"[ok] updated 1688_crawler '{account_name}' "
                  f"with {len(cookies)} cookies")
        else:
            await conn.execute(
                """
                INSERT INTO accounts
                    (id, account_name, platform, identity_group, lifecycle_stage,
                     daily_publish_limit, daily_published_count,
                     health_score, is_active, session_status,
                     cookies_data, created_at, updated_at)
                VALUES (gen_random_uuid(), $1, '1688_crawler', 'crawler', 'nurturing',
                        0, 0, 100, true, 'active',
                        $2, NOW(), NOW())
                """,
                account_name, state_json,
            )
            print(f"[ok] inserted 1688_crawler '{account_name}' "
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

    # Alibaba ecosystem login fingerprint. `cookie2` + `_tb_token_` is
    # the strongest pair; `__cn_logon__` is 1688-specific.
    auth_names = {
        "cookie2", "_tb_token_", "t", "sn", "unb", "_nk_",
        "__cn_logon__", "__cn_logon_id__", "_hvn_login",
        "ali_apache_id", "csg", "l", "v", "lgc",
    }
    found = sorted({c["name"] for c in cookies if c["name"] in auth_names})
    critical = {"cookie2", "_tb_token_", "__cn_logon__"}
    have_critical = bool(critical & set(found))
    if not have_critical:
        print(
            "[warn] no critical auth cookie (cookie2/_tb_token_/__cn_logon__) "
            "found — login may not stick. Did you export .alibaba.com too?",
            file=sys.stderr,
        )
    else:
        print(f"[info] auth cookies present: {found}")

    asyncio.run(_persist(args.mobile, cookies))


if __name__ == "__main__":
    main()
