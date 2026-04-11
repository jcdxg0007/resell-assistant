"""
Session health checker — validates whether saved Playwright sessions are still active.
Opens a headless browser with stored cookies and checks if the platform redirects to login.
"""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from app.services.browser import browser_manager, STATES_DIR
from app.services.platform_login import LOGIN_PAGE_INDICATORS

PLATFORM_CHECK_URLS = {
    "xianyu": "https://www.goofish.com/",
    "xiaohongshu": "https://creator.xiaohongshu.com/",
    "douyin": "https://creator.douyin.com/",
}


async def check_session(account_id: str, platform: str, account_config: dict) -> dict:
    """
    Check whether a saved session is still valid.

    Returns dict with:
      - status: "active" | "expired" | "none"
      - hint: optional reason string
    """
    state_path = STATES_DIR / f"{account_id}.json"
    if not state_path.exists():
        return {"status": "none", "hint": None}

    check_url = PLATFORM_CHECK_URLS.get(platform)
    if not check_url:
        return {"status": "none", "hint": f"不支持的平台: {platform}"}

    page = None
    try:
        if not browser_manager._browser:
            await browser_manager.start()

        context = await browser_manager.get_context(account_id, account_config)
        page = await context.new_page()

        logger.info(f"Session check: opening {check_url} for account {account_id}")
        await page.goto(check_url, wait_until="domcontentloaded", timeout=30000)

        # Poll URL multiple times to handle multi-step redirects
        # (e.g. platform briefly shows /login then auto-redirects back with valid cookies)
        login_indicators = LOGIN_PAGE_INDICATORS.get(platform, [])
        is_on_login = True
        for attempt in range(6):
            await asyncio.sleep(3)
            try:
                current_url = page.url.lower()
            except Exception:
                break
            logger.debug(f"Session check attempt {attempt+1}: {current_url}")
            if not any(ind in current_url for ind in login_indicators):
                is_on_login = False
                break

        final_url = page.url.lower() if not page.is_closed() else ""

        if is_on_login and any(ind in final_url for ind in login_indicators):
            logger.warning(f"Session expired for account {account_id}: stuck on {final_url}")
            await page.close()
            try:
                state_path.unlink()
                logger.info(f"Removed expired state file for {account_id}")
            except Exception:
                pass
            await browser_manager.close_context(account_id)
            return {"status": "expired", "hint": "会话已过期，被重定向到登录页"}
        else:
            logger.info(f"Session active for account {account_id}: on {final_url}")
            await browser_manager.save_state(account_id)
            await page.close()
            return {"status": "active", "hint": None}

    except Exception as e:
        logger.error(f"Session check failed for {account_id}: {e}")
        if page and not page.is_closed():
            try:
                await page.close()
            except Exception:
                pass
        # Network/timeout errors don't necessarily mean expired — keep current status
        return {"status": "none", "hint": f"检查异常: {str(e)[:80]}"}


async def check_all_sessions(accounts: list[dict]) -> dict:
    """
    Check sessions for a list of accounts.

    accounts: list of dicts with keys: id, platform, proxy_url, user_agent, viewport
    Returns summary: {checked, active, expired, skipped, details: [...]}
    """
    summary = {"checked": 0, "active": 0, "expired": 0, "skipped": 0, "details": []}

    for acc in accounts:
        account_id = acc["id"]
        platform = acc["platform"]
        config = {
            "proxy_url": acc.get("proxy_url"),
            "user_agent": acc.get("user_agent"),
            "viewport": acc.get("viewport"),
        }

        result = await check_session(account_id, platform, config)
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

        await asyncio.sleep(1)

    logger.info(
        f"Session check complete: {summary['checked']} checked, "
        f"{summary['active']} active, {summary['expired']} expired, "
        f"{summary['skipped']} skipped"
    )
    return summary
