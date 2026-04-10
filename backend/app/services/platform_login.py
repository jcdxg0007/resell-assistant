"""
Platform login service.
Opens browser to platform login page, captures QR code, polls for success.
"""
import asyncio
import base64
import time
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger

from app.services.browser import browser_manager

STATES_DIR = Path(__file__).parent.parent.parent / "playwright_states"

LOGIN_URLS = {
    "xianyu": "https://login.taobao.com/member/login.jhtml?redirectURL=https%3A%2F%2Fwww.goofish.com%2F",
    "xiaohongshu": "https://creator.xiaohongshu.com/login",
    "douyin": "https://creator.douyin.com/",
}

QR_SELECTORS = {
    "xianyu": [
        'canvas[id="login-qrcode-img"]',
        'img[id="login-qrcode-img"]',
        'div.qrcode-img img',
        '#login img[src*="qrcode"]',
        'canvas',
    ],
    "xiaohongshu": [
        'img.qrcode-img',
        'div.qrcode img',
        'img[class*="qr"]',
        'canvas.qr',
    ],
    "douyin": [
        'img.qrcode-image',
        'div.qrcode img',
        '#login-qrcode img',
    ],
}

SUCCESS_INDICATORS = {
    "xianyu": ["goofish.com", "idle.taobao.com", "xianyu.com", "my.taobao.com"],
    "xiaohongshu": ["creator.xiaohongshu.com/creator", "creator.xiaohongshu.com/publish"],
    "douyin": ["creator.douyin.com/creator-micro"],
}


class LoginStatus(str, Enum):
    IDLE = "idle"
    LOADING = "loading"
    QR_READY = "qr_ready"
    WAITING_SCAN = "waiting_scan"
    SUCCESS = "success"
    FAILED = "failed"
    EXPIRED = "expired"


class LoginSession:
    def __init__(self, account_id: str, platform: str):
        self.account_id = account_id
        self.platform = platform
        self.status = LoginStatus.IDLE
        self.qr_image_b64: str | None = None
        self.page: Any = None
        self.error: str | None = None
        self.created_at = time.time()


_active_sessions: dict[str, LoginSession] = {}


async def start_login(account_id: str, platform: str, account_config: dict) -> LoginSession:
    """Start a login flow: open browser, navigate to login page, capture QR."""
    if account_id in _active_sessions:
        old = _active_sessions[account_id]
        if old.page:
            try:
                await old.page.close()
            except Exception:
                pass

    session = LoginSession(account_id, platform)
    session.status = LoginStatus.LOADING
    _active_sessions[account_id] = session

    try:
        context = await browser_manager.get_context(account_id, account_config)
        page = await context.new_page()
        session.page = page

        login_url = LOGIN_URLS.get(platform)
        if not login_url:
            session.status = LoginStatus.FAILED
            session.error = f"不支持的平台: {platform}"
            return session

        logger.info(f"Navigating to {platform} login for account {account_id}")
        await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        url = page.url.lower()
        if _check_login_success(url, platform):
            session.status = LoginStatus.SUCCESS
            await browser_manager.save_state(account_id)
            logger.info(f"Account {account_id} already logged in to {platform}")
            await page.close()
            session.page = None
            return session

        qr_b64 = await _capture_qr_code(page, platform)
        if qr_b64:
            session.qr_image_b64 = qr_b64
            session.status = LoginStatus.QR_READY
        else:
            screenshot = await page.screenshot(type="png")
            session.qr_image_b64 = base64.b64encode(screenshot).decode()
            session.status = LoginStatus.QR_READY
            logger.warning(f"QR element not found for {platform}, using full page screenshot")

    except Exception as e:
        logger.error(f"Login start failed for {account_id}: {e}")
        session.status = LoginStatus.FAILED
        session.error = str(e)

    return session


async def poll_login_status(account_id: str) -> dict:
    """Check if user has scanned QR and logged in."""
    session = _active_sessions.get(account_id)
    if not session:
        return {"status": LoginStatus.IDLE, "error": "没有进行中的登录"}

    if time.time() - session.created_at > 300:
        session.status = LoginStatus.EXPIRED
        session.error = "登录超时（5分钟），请重新发起"
        if session.page:
            try:
                await session.page.close()
            except Exception:
                pass
            session.page = None
        return _session_to_dict(session)

    if session.status in (LoginStatus.SUCCESS, LoginStatus.FAILED, LoginStatus.EXPIRED):
        return _session_to_dict(session)

    if not session.page or session.page.is_closed():
        session.status = LoginStatus.FAILED
        session.error = "浏览器页面已关闭"
        return _session_to_dict(session)

    try:
        url = session.page.url.lower()

        if _check_login_success(url, session.platform):
            session.status = LoginStatus.SUCCESS
            await browser_manager.save_state(account_id)
            logger.info(f"Account {account_id} login success for {session.platform}")
            await session.page.close()
            session.page = None
            return _session_to_dict(session)

        qr_b64 = await _capture_qr_code(session.page, session.platform)
        if qr_b64 and qr_b64 != session.qr_image_b64:
            session.qr_image_b64 = qr_b64

        session.status = LoginStatus.WAITING_SCAN

    except Exception as e:
        logger.error(f"Poll login error for {account_id}: {e}")
        session.status = LoginStatus.FAILED
        session.error = str(e)

    return _session_to_dict(session)


async def cancel_login(account_id: str):
    """Cancel an in-progress login."""
    session = _active_sessions.pop(account_id, None)
    if session and session.page:
        try:
            await session.page.close()
        except Exception:
            pass


async def get_login_screenshot(account_id: str) -> str | None:
    """Get a fresh full-page screenshot of the login page."""
    session = _active_sessions.get(account_id)
    if not session or not session.page or session.page.is_closed():
        return None
    try:
        screenshot = await session.page.screenshot(type="png")
        return base64.b64encode(screenshot).decode()
    except Exception:
        return None


def _check_login_success(url: str, platform: str) -> bool:
    indicators = SUCCESS_INDICATORS.get(platform, [])
    return any(ind in url for ind in indicators)


async def _capture_qr_code(page: Any, platform: str) -> str | None:
    """Try to locate and screenshot the QR code element."""
    selectors = QR_SELECTORS.get(platform, [])
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=2000):
                screenshot = await el.screenshot(type="png")
                return base64.b64encode(screenshot).decode()
        except Exception:
            continue
    return None


def _session_to_dict(session: LoginSession) -> dict:
    return {
        "status": session.status.value,
        "qr_image": session.qr_image_b64,
        "error": session.error,
        "platform": session.platform,
        "elapsed": int(time.time() - session.created_at),
    }
