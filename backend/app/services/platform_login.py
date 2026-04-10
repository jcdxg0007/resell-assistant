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

# Buttons/tabs to click to switch to QR code login mode
QR_SWITCH_SELECTORS = {
    "xianyu": [
        'text="扫码登录"',
        'a:has-text("扫码登录")',
        'div:has-text("扫码登录")',
        '.qrcode-login',
        '.login-switch:has-text("扫码")',
        '#login >> text="扫码登录"',
        'span:has-text("扫码登录")',
        '.icon-qrcode',
        '[data-action="qrcode_login"]',
    ],
    "xiaohongshu": [
        'text="扫码登录"',
        'div:has-text("扫码登录")',
        'span:has-text("扫码登录")',
        '.qrcode-tab',
        'text="二维码登录"',
    ],
    "douyin": [
        'text="扫码登录"',
        'div:has-text("扫码登录")',
        '.web-login-scan-code',
    ],
}

QR_SELECTORS = {
    "xianyu": [
        '#login-qrcode-img',
        'img[id*="qrcode"]',
        'canvas[id*="qrcode"]',
        'div.qrcode-img img',
        'div.qrcode-img canvas',
        '#login img[src*="qr"]',
        'img[src*="qrcode"]',
        'img[src*="taobao"][src*="qr"]',
        '.qrcode-img',
        'div[class*="qrcode"] img',
        'div[class*="qrcode"] canvas',
        'div[class*="QRCode"] img',
        'div[class*="QRCode"] canvas',
        'canvas',
    ],
    "xiaohongshu": [
        'img.qrcode-img',
        'img[class*="qr"]',
        'img[class*="QR"]',
        'div.qrcode img',
        'div[class*="qrcode"] img',
        'div[class*="qrcode"] canvas',
        'canvas[class*="qr"]',
        'img[src*="qr"]',
    ],
    "douyin": [
        'img.qrcode-image',
        'img[class*="qr"]',
        'div.qrcode img',
        '#login-qrcode img',
        'img[src*="qr"]',
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

        await _switch_to_qr_mode(page, platform)
        await asyncio.sleep(2)

        qr_b64 = await _capture_qr_code(page, platform)
        if not qr_b64:
            # Extra wait + retry: QR might take time to render after mode switch
            await asyncio.sleep(3)
            qr_b64 = await _capture_qr_code(page, platform)

        if not qr_b64:
            # JS fallback: try to find any large img/canvas that looks like a QR
            qr_b64 = await _js_capture_qr(page)

        if qr_b64:
            session.qr_image_b64 = qr_b64
            session.status = LoginStatus.QR_READY
            logger.info(f"QR code captured for {platform} account {account_id}")
        else:
            screenshot = await page.screenshot(type="png", full_page=False)
            session.qr_image_b64 = base64.b64encode(screenshot).decode()
            session.status = LoginStatus.QR_READY
            logger.warning(f"QR element not found for {platform}, using viewport screenshot")

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

        screenshot = await session.page.screenshot(type="png", full_page=False)
        session.qr_image_b64 = base64.b64encode(screenshot).decode()
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
        screenshot = await session.page.screenshot(type="png", full_page=False)
        return base64.b64encode(screenshot).decode()
    except Exception:
        return None


def _check_login_success(url: str, platform: str) -> bool:
    indicators = SUCCESS_INDICATORS.get(platform, [])
    return any(ind in url for ind in indicators)


async def _switch_to_qr_mode(page: Any, platform: str):
    """Try to click the QR code login tab/button to switch to QR scan mode."""
    # Step 1: try static selectors
    selectors = QR_SWITCH_SELECTORS.get(platform, [])
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=1000):
                await el.click()
                logger.info(f"Switched to QR login mode via selector: {selector}")
                await asyncio.sleep(2)
                return
        except Exception:
            continue

    # Step 2: JS-based deep search for QR switch elements
    logger.info(f"Static selectors failed for {platform}, trying JS DOM search...")
    clicked = await page.evaluate("""() => {
        const keywords = ['扫码登录', '二维码登录', '扫码', 'QR', 'qrcode', '其他登录方式'];
        const allEls = document.querySelectorAll('a, button, div, span, p, img, svg, label, li, [role="tab"], [role="button"]');
        for (const el of allEls) {
            const text = (el.textContent || '').trim();
            const cls = (el.className || '').toString().toLowerCase();
            const alt = (el.getAttribute('alt') || '').toLowerCase();
            const title = (el.getAttribute('title') || '').toLowerCase();
            const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
            const combined = `${text} ${cls} ${alt} ${title} ${ariaLabel}`;
            for (const kw of keywords) {
                if (combined.toLowerCase().includes(kw.toLowerCase())) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0 && rect.width < 300) {
                        el.click();
                        return `Clicked: tag=${el.tagName}, text="${text}", class="${cls}", size=${rect.width}x${rect.height}`;
                    }
                }
            }
        }
        // Fallback: look for small images/icons near login form that might be QR toggle
        const icons = document.querySelectorAll('img[src*="qr"], img[src*="scan"], svg[class*="qr"], svg[class*="scan"], [class*="icon-qr"], [class*="icon-scan"], [class*="other-login"], [class*="switch-login"]');
        for (const el of icons) {
            const rect = el.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
                el.click();
                return `Clicked icon: tag=${el.tagName}, class="${el.className}", size=${rect.width}x${rect.height}`;
            }
        }
        return null;
    }""")

    if clicked:
        logger.info(f"JS DOM search result: {clicked}")
        await asyncio.sleep(2)
    else:
        # Step 3: log page structure for debugging
        debug_info = await page.evaluate("""() => {
            const clickables = document.querySelectorAll('a, button, [role="tab"], [role="button"], img, svg');
            const info = [];
            for (const el of clickables) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    info.push({
                        tag: el.tagName,
                        text: (el.textContent || '').trim().substring(0, 50),
                        cls: (el.className || '').toString().substring(0, 80),
                        src: (el.getAttribute('src') || '').substring(0, 80),
                        size: `${Math.round(rect.width)}x${Math.round(rect.height)}`
                    });
                }
            }
            return info.slice(0, 30);
        }""")
        logger.warning(f"No QR switch found for {platform}. Page clickable elements: {debug_info}")


async def _js_capture_qr(page: Any) -> str | None:
    """Use JS to find any QR-like image/canvas on the page and screenshot it."""
    try:
        qr_el = await page.evaluate_handle("""() => {
            // Look for img with QR-like src or near QR-related parent
            const imgs = document.querySelectorAll('img');
            for (const img of imgs) {
                const rect = img.getBoundingClientRect();
                const src = (img.src || '').toLowerCase();
                const cls = (img.className || '').toString().toLowerCase();
                const parentCls = (img.parentElement?.className || '').toString().toLowerCase();
                const isQR = src.includes('qr') || src.includes('scan') ||
                             cls.includes('qr') || parentCls.includes('qr') ||
                             cls.includes('code') || parentCls.includes('code');
                if (isQR && rect.width >= 80 && rect.height >= 80) return img;
                // Square images around 120-300px are likely QR codes
                if (rect.width >= 100 && rect.height >= 100 &&
                    Math.abs(rect.width - rect.height) < 20) return img;
            }
            // Check canvas elements
            const canvases = document.querySelectorAll('canvas');
            for (const c of canvases) {
                const rect = c.getBoundingClientRect();
                if (rect.width >= 80 && rect.height >= 80) return c;
            }
            return null;
        }""")
        el = qr_el.as_element()
        if el:
            box = await el.bounding_box()
            if box and box['width'] >= 80 and box['height'] >= 80:
                screenshot = await el.screenshot(type="png")
                logger.info(f"JS QR capture: {box['width']}x{box['height']}")
                return base64.b64encode(screenshot).decode()
    except Exception as e:
        logger.debug(f"JS QR capture failed: {e}")
    return None


async def _capture_qr_code(page: Any, platform: str) -> str | None:
    """Try to locate and screenshot the QR code element."""
    selectors = QR_SELECTORS.get(platform, [])
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if await el.is_visible(timeout=1500):
                box = await el.bounding_box()
                if box and box['width'] > 50 and box['height'] > 50:
                    screenshot = await el.screenshot(type="png")
                    logger.info(f"QR captured via selector: {selector} ({box['width']}x{box['height']})")
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
