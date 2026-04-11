"""
Session health checker — cookie-based offline check (no network needed).
Reads saved Playwright state from file or database and checks cookie expiration.
"""
import json
import time

from loguru import logger

from app.services.browser import STATES_DIR, _load_cookies_from_db

PLATFORM_SESSION_COOKIES = {
    "xianyu": [
        "cookie2", "_m_h5_tk", "sgcookie", "XSRF-TOKEN",
        "munb", "csg", "_tb_token_",
    ],
    "xiaohongshu": [
        "web_session", "xsecappid", "a1", "webId",
        "galaxy_creator_session_id", "customerClientId",
    ],
    "douyin": [
        "sessionid", "sid_tt", "uid_tt", "passport_csrf_token",
    ],
}


def _analyze_cookies(cookies: list[dict], platform: str) -> dict:
    """Analyze cookie list and return session status."""
    if not cookies:
        return {"status": "expired", "hint": "无有效 cookies"}

    now = time.time()
    session_cookie_names = PLATFORM_SESSION_COOKIES.get(platform, [])

    total_cookies = len(cookies)
    expired_cookies = 0
    valid_session_cookies = 0
    expired_session_cookies = 0
    earliest_session_expiry = None

    for cookie in cookies:
        expires = cookie.get("expires", -1)
        name = cookie.get("name", "")

        is_expired = (expires > 0 and expires < now)
        if is_expired:
            expired_cookies += 1

        is_session_cookie = any(
            sc.lower() in name.lower() for sc in session_cookie_names
        ) if session_cookie_names else False

        if is_session_cookie:
            if is_expired:
                expired_session_cookies += 1
            else:
                valid_session_cookies += 1
                if expires > 0:
                    if earliest_session_expiry is None or expires < earliest_session_expiry:
                        earliest_session_expiry = expires

    if session_cookie_names:
        if valid_session_cookies > 0:
            hint = None
            if earliest_session_expiry:
                remaining_seconds = earliest_session_expiry - now
                remaining_hours = remaining_seconds / 3600
                if remaining_hours < 24:
                    hint = f"会话将在 {remaining_hours:.0f} 小时后过期"
                else:
                    days = remaining_hours / 24
                    hint = f"会话有效期剩余约 {days:.0f} 天"
            logger.info(
                f"Cookie analysis: {valid_session_cookies} valid session cookies, "
                f"{expired_session_cookies} expired"
            )
            return {"status": "active", "hint": hint}
        elif expired_session_cookies > 0:
            return {"status": "expired", "hint": "会话 cookies 已过期，请重新登录"}

    valid_cookies = total_cookies - expired_cookies
    if valid_cookies == 0:
        return {"status": "expired", "hint": "所有 cookies 已过期"}

    expiry_ratio = expired_cookies / total_cookies if total_cookies > 0 else 0
    if expiry_ratio > 0.8:
        return {"status": "expired", "hint": f"大部分 cookies 已过期 ({expired_cookies}/{total_cookies})"}

    return {"status": "active", "hint": f"{valid_cookies}/{total_cookies} cookies 有效"}


def check_session_offline(account_id: str, platform: str) -> dict:
    """
    Check session validity by reading saved cookies from file or database.
    No browser or network access needed.
    """
    state_path = STATES_DIR / f"{account_id}.json"
    state_json = None

    # Try file first
    if state_path.exists():
        try:
            state_json = state_path.read_text()
        except Exception as e:
            logger.error(f"Failed to read state file for {account_id}: {e}")

    # Fallback: try database
    if not state_json:
        try:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(_load_cookies_sync, account_id)
                    state_json = future.result(timeout=5)
            else:
                new_loop = asyncio.new_event_loop()
                try:
                    state_json = new_loop.run_until_complete(_load_cookies_from_db(account_id))
                finally:
                    new_loop.close()

            if state_json:
                logger.info(f"Loaded cookies from DB for {account_id}")
                # Restore file cache
                try:
                    state_path.write_text(state_json)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Failed to load cookies from DB for {account_id}: {e}")

    if not state_json:
        return {"status": "none", "hint": None}

    try:
        state_data = json.loads(state_json)
    except Exception as e:
        logger.error(f"Failed to parse state for {account_id}: {e}")
        return {"status": "none", "hint": f"状态数据解析失败: {str(e)[:50]}"}

    cookies = state_data.get("cookies", [])
    result = _analyze_cookies(cookies, platform)
    logger.info(f"Session check for {account_id}: {result['status']}")
    return result


def _load_cookies_sync(account_id: str) -> str | None:
    """Load cookies from DB in a new event loop (for use from sync context)."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_load_cookies_from_db(account_id))
    finally:
        loop.close()


async def check_session(account_id: str, platform: str, account_config: dict) -> dict:
    """Async wrapper — tries DB directly first."""
    state_path = STATES_DIR / f"{account_id}.json"
    state_json = None

    if state_path.exists():
        try:
            state_json = state_path.read_text()
        except Exception:
            pass

    if not state_json:
        state_json = await _load_cookies_from_db(account_id)
        if state_json:
            logger.info(f"Loaded cookies from DB for {account_id}")
            try:
                state_path.write_text(state_json)
            except Exception:
                pass

    if not state_json:
        return {"status": "none", "hint": None}

    try:
        state_data = json.loads(state_json)
    except Exception:
        return {"status": "none", "hint": "状态数据解析失败"}

    cookies = state_data.get("cookies", [])
    return _analyze_cookies(cookies, platform)


async def check_all_sessions(accounts: list[dict]) -> dict:
    """Check sessions for a list of accounts."""
    summary = {"checked": 0, "active": 0, "expired": 0, "skipped": 0, "details": []}

    for acc in accounts:
        account_id = acc["id"]
        platform = acc["platform"]

        result = await check_session(account_id, platform, {})
        status = result["status"]

        if status == "none":
            summary["skipped"] += 1
        elif status == "active":
            summary["active"] += 1
            summary["checked"] += 1
        elif status == "expired":
            summary["expired"] += 1
            summary["checked"] += 1

        summary["details"].append({
            "account_id": account_id,
            "platform": platform,
            **result,
        })

    logger.info(
        f"Session check complete: {summary['checked']} checked, "
        f"{summary['active']} active, {summary['expired']} expired, "
        f"{summary['skipped']} skipped"
    )
    return summary
