"""
Session health checker — cookie-based offline check (no network needed).
Reads saved Playwright state files and checks cookie expiration times.
"""
import json
import time
from pathlib import Path

from loguru import logger

from app.services.browser import STATES_DIR

# Key cookies per platform that indicate a valid login session
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


def check_session_offline(account_id: str, platform: str) -> dict:
    """
    Check session validity by reading the saved cookie state file.
    No browser or network access needed.

    Returns dict with:
      - status: "active" | "expired" | "none"
      - hint: optional reason string
      - details: dict with cookie analysis
    """
    state_path = STATES_DIR / f"{account_id}.json"
    if not state_path.exists():
        return {"status": "none", "hint": None}

    try:
        state_data = json.loads(state_path.read_text())
    except Exception as e:
        logger.error(f"Failed to read state file for {account_id}: {e}")
        return {"status": "none", "hint": f"状态文件读取失败: {str(e)[:50]}"}

    cookies = state_data.get("cookies", [])
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

    # Decision logic
    if session_cookie_names:
        # Platform has known session cookies: check those specifically
        if valid_session_cookies > 0:
            remaining_hours = None
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
                f"Session active for {account_id}: "
                f"{valid_session_cookies} valid session cookies, "
                f"{expired_session_cookies} expired"
            )
            return {"status": "active", "hint": hint}
        elif expired_session_cookies > 0:
            logger.warning(f"Session expired for {account_id}: all session cookies expired")
            return {"status": "expired", "hint": "会话 cookies 已过期，请重新登录"}
        else:
            # No session cookies found at all — fallback to general check
            pass

    # Fallback: general cookie analysis
    valid_cookies = total_cookies - expired_cookies
    if valid_cookies == 0:
        return {"status": "expired", "hint": "所有 cookies 已过期"}

    expiry_ratio = expired_cookies / total_cookies if total_cookies > 0 else 0
    if expiry_ratio > 0.8:
        return {"status": "expired", "hint": f"大部分 cookies 已过期 ({expired_cookies}/{total_cookies})"}

    logger.info(f"Session likely active for {account_id}: {valid_cookies}/{total_cookies} cookies valid")
    return {"status": "active", "hint": f"{valid_cookies}/{total_cookies} cookies 有效"}


# Keep async interface for compatibility with API/Celery callers
async def check_session(account_id: str, platform: str, account_config: dict) -> dict:
    """Async wrapper around the offline cookie check."""
    return check_session_offline(account_id, platform)


async def check_all_sessions(accounts: list[dict]) -> dict:
    """
    Check sessions for a list of accounts.
    Returns summary: {checked, active, expired, skipped, details: [...]}
    """
    summary = {"checked": 0, "active": 0, "expired": 0, "skipped": 0, "details": []}

    for acc in accounts:
        account_id = acc["id"]
        platform = acc["platform"]

        result = check_session_offline(account_id, platform)
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
