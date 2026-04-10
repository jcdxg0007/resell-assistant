"""
Platform login service — interactive phone + SMS code flow.
Opens browser to login page, fills phone / code via Playwright, polls for success.
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

SUCCESS_INDICATORS = {
    "xianyu": ["goofish.com", "idle.taobao.com", "xianyu.com", "my.taobao.com"],
    "xiaohongshu": ["creator.xiaohongshu.com/creator", "creator.xiaohongshu.com/publish"],
    "douyin": ["creator.douyin.com/creator-micro"],
}

PHONE_INPUT_JS = """() => {
    const inputs = document.querySelectorAll('input');
    for (const inp of inputs) {
        const ph = (inp.placeholder || '').toLowerCase();
        const name = (inp.name || '').toLowerCase();
        const type = (inp.type || '').toLowerCase();
        const id = (inp.id || '').toLowerCase();
        const label = (inp.getAttribute('aria-label') || '').toLowerCase();
        if (type === 'tel') return inp;
        if (ph.includes('手机') || ph.includes('phone') || ph.includes('号码')) return inp;
        if (name.includes('phone') || name.includes('mobile') || name.includes('tel')) return inp;
        if (id.includes('phone') || id.includes('mobile') || id.includes('tel')) return inp;
        if (label.includes('手机') || label.includes('phone')) return inp;
    }
    // fallback: first visible text/tel/number input
    for (const inp of inputs) {
        const type = (inp.type || 'text').toLowerCase();
        if (['text', 'tel', 'number'].includes(type)) {
            const rect = inp.getBoundingClientRect();
            if (rect.width > 50 && rect.height > 0) return inp;
        }
    }
    return null;
}"""

SEND_CODE_JS = """() => {
    const keywords = ['发送验证码', '获取验证码', '发送', 'send', '获取短信'];
    const allEls = document.querySelectorAll('button, a, div, span, [role="button"]');
    for (const el of allEls) {
        const text = (el.textContent || '').trim();
        for (const kw of keywords) {
            if (text.includes(kw)) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    el.click();
                    return `Clicked: "${text}"`;
                }
            }
        }
    }
    return null;
}"""

CODE_INPUT_JS = """() => {
    const inputs = document.querySelectorAll('input');
    const candidates = [];
    for (const inp of inputs) {
        const ph = (inp.placeholder || '').toLowerCase();
        const name = (inp.name || '').toLowerCase();
        const id = (inp.id || '').toLowerCase();
        const label = (inp.getAttribute('aria-label') || '').toLowerCase();
        const type = (inp.type || 'text').toLowerCase();
        if (ph.includes('验证码') || ph.includes('code') || ph.includes('verification')) return inp;
        if (name.includes('code') || name.includes('captcha') || name.includes('sms')) return inp;
        if (id.includes('code') || id.includes('captcha') || id.includes('sms')) return inp;
        if (label.includes('验证码') || label.includes('code')) return inp;
        // Collect numeric-looking inputs as fallback
        if (['text', 'tel', 'number', 'password'].includes(type)) {
            const rect = inp.getBoundingClientRect();
            if (rect.width > 30 && rect.height > 0) candidates.push(inp);
        }
    }
    // Return the second visible input (first is usually phone)
    return candidates.length >= 2 ? candidates[1] : null;
}"""

LOGIN_BUTTON_JS = """() => {
    const buttons = document.querySelectorAll('button, [role="button"], input[type="submit"]');
    for (const btn of buttons) {
        const text = (btn.textContent || btn.value || '').trim();
        const cls = (btn.className || '').toString().toLowerCase();
        if ((text === '登录' || text === '登 录' || text.toLowerCase() === 'log in' || text.toLowerCase() === 'login' || text === '注册/登录')
            && !cls.includes('disabled')) {
            const rect = btn.getBoundingClientRect();
            if (rect.width > 50 && rect.height > 20) {
                btn.click();
                return `Clicked: "${text}"`;
            }
        }
    }
    // broader search
    const allEls = document.querySelectorAll('button, a, div, [role="button"]');
    for (const el of allEls) {
        const text = (el.textContent || '').trim();
        if (text === '登录' || text === '登 录') {
            const rect = el.getBoundingClientRect();
            if (rect.width > 50 && rect.height > 20 && rect.width < 400) {
                el.click();
                return `Clicked (broad): "${text}"`;
            }
        }
    }
    return null;
}"""


class LoginStatus(str, Enum):
    IDLE = "idle"
    LOADING = "loading"
    PAGE_READY = "page_ready"
    CODE_SENT = "code_sent"
    SUBMITTING = "submitting"
    SUCCESS = "success"
    FAILED = "failed"
    EXPIRED = "expired"


class LoginSession:
    def __init__(self, account_id: str, platform: str):
        self.account_id = account_id
        self.platform = platform
        self.status = LoginStatus.IDLE
        self.screenshot_b64: str | None = None
        self.page: Any = None
        self.error: str | None = None
        self.created_at = time.time()


_active_sessions: dict[str, LoginSession] = {}


async def start_login(account_id: str, platform: str, account_config: dict) -> LoginSession:
    """Open browser and navigate to platform login page."""
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

        # Ensure we're on SMS login tab (for platforms that default elsewhere)
        await _switch_to_sms_mode(page, platform)
        await asyncio.sleep(1)

        screenshot = await page.screenshot(type="png", full_page=False)
        session.screenshot_b64 = base64.b64encode(screenshot).decode()
        session.status = LoginStatus.PAGE_READY
        logger.info(f"Login page ready for {platform} account {account_id}")

    except Exception as e:
        logger.error(f"Login start failed for {account_id}: {e}")
        session.status = LoginStatus.FAILED
        session.error = str(e)

    return session


async def send_sms_code(account_id: str, phone: str) -> dict:
    """Fill the phone number and click 'send verification code'."""
    session = _active_sessions.get(account_id)
    if not session or not session.page or session.page.is_closed():
        return {"success": False, "error": "没有进行中的登录会话"}

    try:
        page = session.page

        # Find and fill phone input via Playwright locators (real interaction)
        phone_filled = await _fill_phone_input(page, phone)
        if not phone_filled:
            screenshot = await page.screenshot(type="png", full_page=False)
            session.screenshot_b64 = base64.b64encode(screenshot).decode()
            return {"success": False, "error": "未找到手机号输入框", "screenshot": session.screenshot_b64}
        logger.info(f"Phone number filled for {account_id}")
        await asyncio.sleep(0.5)

        # Click send code button via Playwright (real mouse click)
        code_clicked = await _click_send_code(page)
        if not code_clicked:
            screenshot = await page.screenshot(type="png", full_page=False)
            session.screenshot_b64 = base64.b64encode(screenshot).decode()
            return {"success": False, "error": "未找到「发送验证码」按钮", "screenshot": session.screenshot_b64}

        await asyncio.sleep(2)
        screenshot = await page.screenshot(type="png", full_page=False)
        session.screenshot_b64 = base64.b64encode(screenshot).decode()
        session.status = LoginStatus.CODE_SENT
        return {"success": True, "screenshot": session.screenshot_b64}

    except Exception as e:
        logger.error(f"Send SMS code failed for {account_id}: {e}")
        return {"success": False, "error": str(e)}


async def submit_login_code(account_id: str, code: str) -> dict:
    """Fill verification code and click login."""
    session = _active_sessions.get(account_id)
    if not session or not session.page or session.page.is_closed():
        return {"success": False, "error": "没有进行中的登录会话"}

    try:
        page = session.page
        session.status = LoginStatus.SUBMITTING

        # Find and fill code input via Playwright locators
        code_filled = await _fill_code_input(page, code)
        if not code_filled:
            screenshot = await page.screenshot(type="png", full_page=False)
            session.screenshot_b64 = base64.b64encode(screenshot).decode()
            return {"success": False, "error": "未找到验证码输入框", "screenshot": session.screenshot_b64}
        logger.info(f"Code filled for {account_id}")
        await asyncio.sleep(0.5)

        # Click login button via Playwright
        login_clicked = await _click_login_button(page)
        if not login_clicked:
            logger.warning("Login button not found, trying Enter key")
            await page.keyboard.press("Enter")

        # Wait for navigation
        for _ in range(10):
            await asyncio.sleep(2)
            try:
                url = page.url.lower()
            except Exception:
                break
            if _check_login_success(url, session.platform):
                session.status = LoginStatus.SUCCESS
                await browser_manager.save_state(account_id)
                logger.info(f"Account {account_id} login success!")
                try:
                    await page.close()
                except Exception:
                    pass
                session.page = None
                return {"success": True, "status": "success"}

        # Not redirected yet — take screenshot
        screenshot = await page.screenshot(type="png", full_page=False)
        session.screenshot_b64 = base64.b64encode(screenshot).decode()

        # Check for error messages on page
        error_text = await page.evaluate("""() => {
            const errs = document.querySelectorAll('.error, .err-msg, [class*="error"], [class*="alert"], [class*="warn"], [class*="tip"]');
            for (const el of errs) {
                const text = (el.textContent || '').trim();
                if (text && text.length < 100 && text.length > 2) return text;
            }
            return null;
        }""")

        if error_text:
            session.status = LoginStatus.FAILED
            session.error = error_text
            return {"success": False, "error": error_text, "screenshot": session.screenshot_b64}

        session.status = LoginStatus.PAGE_READY
        return {"success": False, "error": "登录未完成，请检查页面状态", "screenshot": session.screenshot_b64}

    except Exception as e:
        logger.error(f"Submit login code failed for {account_id}: {e}")
        session.status = LoginStatus.FAILED
        session.error = str(e)
        return {"success": False, "error": str(e)}


async def poll_login_status(account_id: str) -> dict:
    """Check current login session status."""
    session = _active_sessions.get(account_id)
    if not session:
        return {"status": LoginStatus.IDLE, "error": "没有进行中的登录"}

    if time.time() - session.created_at > 600:
        session.status = LoginStatus.EXPIRED
        session.error = "登录超时（10分钟），请重新发起"
        if session.page:
            try:
                await session.page.close()
            except Exception:
                pass
            session.page = None
        return _session_to_dict(session)

    if session.status in (LoginStatus.SUCCESS, LoginStatus.FAILED, LoginStatus.EXPIRED):
        return _session_to_dict(session)

    if session.page and not session.page.is_closed():
        try:
            url = session.page.url.lower()
            if _check_login_success(url, session.platform):
                session.status = LoginStatus.SUCCESS
                await browser_manager.save_state(account_id)
                await session.page.close()
                session.page = None
        except Exception:
            pass

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
    """Get a fresh screenshot of the login page."""
    session = _active_sessions.get(account_id)
    if not session or not session.page or session.page.is_closed():
        return None
    try:
        screenshot = await session.page.screenshot(type="png", full_page=False)
        b64 = base64.b64encode(screenshot).decode()
        session.screenshot_b64 = b64
        return b64
    except Exception:
        return None


async def _fill_phone_input(page: Any, phone: str) -> bool:
    """Find the phone input and fill it using Playwright native interactions."""
    # Strategy 1: Playwright locators by placeholder/role
    strategies = [
        page.locator('input[type="tel"]'),
        page.get_by_placeholder("手机号"),
        page.get_by_placeholder("请输入手机号"),
        page.get_by_placeholder("手机号码"),
        page.get_by_placeholder("输入手机号"),
        page.locator('input[placeholder*="手机"]'),
        page.locator('input[placeholder*="phone"]'),
        page.locator('input[name*="phone"]'),
        page.locator('input[name*="mobile"]'),
        page.locator('input[id*="phone"]'),
        page.locator('input[id*="mobile"]'),
    ]
    for loc in strategies:
        try:
            if await loc.first.is_visible(timeout=800):
                await loc.first.click()
                await loc.first.fill("")
                await loc.first.fill(phone)
                logger.info(f"Phone filled via Playwright locator")
                return True
        except Exception:
            continue

    # Strategy 2: JS to find, then Playwright to interact
    try:
        handle = await page.evaluate_handle(PHONE_INPUT_JS)
        el = handle.as_element()
        if el:
            await el.click()
            await el.fill("")
            await el.fill(phone)
            logger.info("Phone filled via JS handle + Playwright fill")
            return True
    except Exception as e:
        logger.debug(f"JS phone fill failed: {e}")

    return False


async def _click_send_code(page: Any) -> bool:
    """Find and click the 'send verification code' button using Playwright."""
    # Strategy 1: Playwright text-based locators (triggers real mouse events)
    text_patterns = [
        "发送验证码", "获取验证码", "发送短信验证码", "获取短信验证码",
    ]
    for text in text_patterns:
        try:
            loc = page.get_by_text(text, exact=True)
            if await loc.first.is_visible(timeout=800):
                await loc.first.click()
                logger.info(f"Send code clicked via get_by_text('{text}')")
                return True
        except Exception:
            continue

    # Strategy 2: partial text match
    for text in text_patterns:
        try:
            loc = page.get_by_text(text)
            if await loc.first.is_visible(timeout=800):
                await loc.first.click()
                logger.info(f"Send code clicked via partial text '{text}'")
                return True
        except Exception:
            continue

    # Strategy 3: role-based
    for text in text_patterns:
        try:
            loc = page.get_by_role("button", name=text)
            if await loc.first.is_visible(timeout=800):
                await loc.first.click()
                logger.info(f"Send code clicked via role button '{text}'")
                return True
        except Exception:
            continue
        try:
            loc = page.get_by_role("link", name=text)
            if await loc.first.is_visible(timeout=800):
                await loc.first.click()
                logger.info(f"Send code clicked via role link '{text}'")
                return True
        except Exception:
            continue

    # Strategy 4: CSS selectors
    css_selectors = [
        'button:has-text("发送验证码")',
        'a:has-text("发送验证码")',
        'span:has-text("发送验证码")',
        'div:has-text("发送验证码")',
        'button:has-text("获取验证码")',
        'a:has-text("获取验证码")',
        'span:has-text("获取验证码")',
    ]
    for sel in css_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=800):
                await loc.click()
                logger.info(f"Send code clicked via CSS '{sel}'")
                return True
        except Exception:
            continue

    # Strategy 5: JS find + Playwright click (get coords and click)
    try:
        coords = await page.evaluate("""() => {
            const keywords = ['发送验证码', '获取验证码'];
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
                const text = walker.currentNode.textContent.trim();
                for (const kw of keywords) {
                    if (text.includes(kw)) {
                        const el = walker.currentNode.parentElement;
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, text: text };
                        }
                    }
                }
            }
            return null;
        }""")
        if coords:
            await page.mouse.click(coords['x'], coords['y'])
            logger.info(f"Send code clicked via coordinates: ({coords['x']}, {coords['y']}) text='{coords['text']}'")
            return True
    except Exception as e:
        logger.debug(f"JS coords click failed: {e}")

    return False


async def _fill_code_input(page: Any, code: str) -> bool:
    """Find the verification code input and fill it."""
    strategies = [
        page.get_by_placeholder("验证码"),
        page.get_by_placeholder("请输入验证码"),
        page.get_by_placeholder("输入验证码"),
        page.get_by_placeholder("短信验证码"),
        page.locator('input[placeholder*="验证码"]'),
        page.locator('input[placeholder*="code"]'),
        page.locator('input[name*="code"]'),
        page.locator('input[name*="captcha"]'),
        page.locator('input[id*="code"]'),
    ]
    for loc in strategies:
        try:
            if await loc.first.is_visible(timeout=800):
                await loc.first.click()
                await loc.first.fill("")
                await loc.first.fill(code)
                logger.info("Code filled via Playwright locator")
                return True
        except Exception:
            continue

    # JS fallback
    try:
        handle = await page.evaluate_handle(CODE_INPUT_JS)
        el = handle.as_element()
        if el:
            await el.click()
            await el.fill("")
            await el.fill(code)
            logger.info("Code filled via JS handle + Playwright fill")
            return True
    except Exception as e:
        logger.debug(f"JS code fill failed: {e}")

    return False


async def _click_login_button(page: Any) -> bool:
    """Find and click the login/submit button."""
    text_patterns = ["登录", "登 录", "Log in", "Login"]
    for text in text_patterns:
        try:
            loc = page.get_by_role("button", name=text)
            if await loc.first.is_visible(timeout=800):
                await loc.first.click()
                logger.info(f"Login button clicked via role '{text}'")
                return True
        except Exception:
            continue

    css_selectors = [
        'button:has-text("登录")',
        'button:has-text("登 录")',
        'button[type="submit"]',
        'input[type="submit"]',
    ]
    for sel in css_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=800):
                await loc.click()
                logger.info(f"Login button clicked via CSS '{sel}'")
                return True
        except Exception:
            continue

    # JS coordinates fallback
    try:
        coords = await page.evaluate("""() => {
            const buttons = document.querySelectorAll('button, [role="button"], input[type="submit"]');
            for (const btn of buttons) {
                const text = (btn.textContent || btn.value || '').trim();
                if (text === '登录' || text === '登 录') {
                    const rect = btn.getBoundingClientRect();
                    if (rect.width > 50 && rect.height > 20) {
                        return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
                    }
                }
            }
            return null;
        }""")
        if coords:
            await page.mouse.click(coords['x'], coords['y'])
            logger.info(f"Login button clicked via coordinates")
            return True
    except Exception as e:
        logger.debug(f"JS login click failed: {e}")

    return False


def _check_login_success(url: str, platform: str) -> bool:
    indicators = SUCCESS_INDICATORS.get(platform, [])
    return any(ind in url for ind in indicators)


async def _switch_to_sms_mode(page: Any, platform: str):
    """Ensure we're on the SMS/phone login tab."""
    try:
        clicked = await page.evaluate("""() => {
            const keywords = ['短信验证登录', '短信登录', '验证码登录', '手机验证码登录', '手机号登录'];
            const allEls = document.querySelectorAll('a, button, div, span, li, [role="tab"], [role="button"]');
            for (const el of allEls) {
                const text = (el.textContent || '').trim();
                for (const kw of keywords) {
                    if (text === kw || text.includes(kw)) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0 && rect.width < 300) {
                            el.click();
                            return `Clicked: "${text}"`;
                        }
                    }
                }
            }
            return null;
        }""")
        if clicked:
            logger.info(f"Switched to SMS mode: {clicked}")
            await asyncio.sleep(1)
    except Exception as e:
        logger.debug(f"Switch to SMS mode: {e}")


def _session_to_dict(session: LoginSession) -> dict:
    return {
        "status": session.status.value,
        "screenshot": session.screenshot_b64,
        "error": session.error,
        "platform": session.platform,
        "elapsed": int(time.time() - session.created_at),
    }
