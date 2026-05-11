"""
PDD 小号登录辅助脚本（两阶段）

用法:
  1) 发送验证码：
       python scripts/pdd_login.py send --mobile 19955661876
  2) 填验证码并登录 + 入库：
       python scripts/pdd_login.py verify --mobile 19955661876 --otp 123456

两阶段之间浏览器会话通过 user_data_dir 持久化到磁盘
(/tmp/pdd_login_profile)，所以验证码请求和提交必须在同一
台机器、同一个目录下完成。

登录成功后会把 storage_state 写入 accounts 表，约定：
    platform='pdd_crawler'
    account_name='pdd_crawler_<mobile后4位>'
instant_search 会按 platform='pdd_crawler' 的规则自动加载该账号的
cookies 给 PDD 爬虫使用。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from loguru import logger


PROFILE_DIR = Path("/tmp/pdd_login_profile")
# Use the H5 site — the PC login page forces sliding captcha more
# aggressively than the mobile site, and our server IP looks cleaner
# from a mobile user-agent.
LOGIN_URL = "https://mobile.yangkeduo.com/login.html"
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.6 "
    "Mobile/15E148 Safari/604.1"
)


async def _launch(playwright, *, headless: bool = True):
    """Persistent iPhone-emulated context. Reusing the same profile dir
    across the two phases keeps the captcha-binding cookies alive.

    Uses the system chromium (``/usr/bin/chromium``) because the
    Playwright bundle on this host ships only the headless-shell binary,
    which refuses to launch in persistent-context mode.
    """
    import shutil
    system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    kwargs: dict = dict(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        user_agent=USER_AGENT,
        viewport={"width": 390, "height": 844},
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        # No proxy — server IP is CN (aliyun beijing). PDD blocks overseas
        # IPs on the login endpoint outright.
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    if system_chromium:
        kwargs["executable_path"] = system_chromium
        logger.info(f"using system chromium: {system_chromium}")
    return await playwright.chromium.launch_persistent_context(**kwargs)


async def _dump_state_and_screenshot(page, tag: str) -> None:
    try:
        shot = f"/tmp/pdd_login_{tag}_{int(time.time())}.png"
        await page.screenshot(path=shot, full_page=True)
        logger.info(f"saved screenshot → {shot}")
    except Exception:
        pass


async def _find_mobile_input(page):
    """PDD's login page renames its inputs every so often. Try several
    selectors; return the first that becomes visible."""
    candidates = [
        'input[type="tel"]',
        'input[placeholder*="手机号"]',
        'input[name="mobile"]',
        'input[maxlength="11"]',
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=3000)
            return loc
        except Exception:
            continue
    return None


async def _find_otp_input(page):
    candidates = [
        'input[placeholder*="验证码"]',
        'input[maxlength="6"]',
        'input[name="code"]',
        'input[name="captcha"]',
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=3000)
            return loc
        except Exception:
            continue
    return None


async def _click_by_text(page, texts: list[str], timeout: int = 3000) -> bool:
    """Click the first button whose visible text matches one of ``texts``."""
    for t in texts:
        try:
            loc = page.get_by_text(t, exact=False).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click(timeout=2000)
            return True
        except Exception:
            continue
    return False


async def phase_send(mobile: str) -> int:
    """Open the login page, fill mobile, request the OTP. Stores the
    captcha-bound session in PROFILE_DIR for phase_verify to reuse.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        ctx = await _launch(p)
        try:
            page = await ctx.new_page()
            logger.info(f"navigating {LOGIN_URL}")
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(3)

            # H5 landing page shows two cards inside ``div.login-box``:
            # the red "打开拼多多APP" and the outlined "手机登录". The second
            # one is the one we want. ``get_by_text`` occasionally clicks a
            # non-interactive inner span, so drop down to evaluate() to
            # locate the clickable ancestor and dispatch the click directly.
            clicked_phone = await page.evaluate("""
() => {
  const box = document.querySelector('.login-box');
  if (!box) return false;
  // Iterate the direct card children; prefer the one whose text
  // contains 手机 (vs 拼多多APP).
  for (const child of box.children) {
    const txt = (child.innerText || '').trim();
    if (txt.includes('手机') || txt.includes('登录') && !txt.includes('APP')) {
      child.click();
      return true;
    }
  }
  // Fallback: click the 2nd card unconditionally (landing always has 2).
  if (box.children.length >= 2) {
    box.children[1].click();
    return true;
  }
  return false;
}
""")
            if not clicked_phone:
                await _dump_state_and_screenshot(page, "no_sms_tab")
                logger.error(
                    "'手机登录' entry button not found. Page may have changed."
                )
                return 2
            logger.info("clicked '手机登录' entry card")
            await asyncio.sleep(3)

            mobile_input = await _find_mobile_input(page)
            if not mobile_input:
                await _dump_state_and_screenshot(page, "no_mobile_input")
                logger.error("mobile input not found. Inspect the screenshot.")
                return 2
            await mobile_input.fill(mobile)
            logger.info(f"filled mobile {mobile}")
            await asyncio.sleep(1)

            # PDD H5 gates the SMS send on the "同意服务协议" checkbox. If
            # it's unchecked the send-OTP click is silently dropped.
            try:
                ticked = await page.evaluate("""
() => {
  // PDD H5 uses <i class="agreement-icon"> as the clickable dot.
  const icon = document.querySelector('.agreement-icon');
  if (icon) {
    icon.click();
    return 'icon';
  }
  const wrap = document.querySelector('.agreement-wrap');
  if (wrap) {
    wrap.click();
    return 'wrap';
  }
  return 'not_found';
}
""")
                logger.info(f"agreement checkbox tick: {ticked}")
                await asyncio.sleep(1)
            except Exception as e:
                logger.warning(f"agreement tick failed: {e}")

            # "发送验证码" button lives inside form.login-ui-form. Use JS so
            # we don't accidentally pick a disabled/overlayed copy.
            clicked = await page.evaluate("""
() => {
  const form = document.querySelector('form.login-ui-form') || document.querySelector('.container');
  if (!form) return false;
  for (const b of form.querySelectorAll('button')) {
    const t = (b.innerText || '').trim();
    if (t.includes('发送') || t.includes('获取')) {
      if (b.disabled) return 'disabled';
      b.click();
      return true;
    }
  }
  return false;
}
""")
            if clicked != True:
                await _dump_state_and_screenshot(page, "no_send_btn")
                logger.error(f"send-OTP button not clickable (state={clicked}).")
                return 3
            logger.info("clicked send-OTP button, waiting 5s to see what happens")
            await asyncio.sleep(5)

            # Detect slider captcha
            body = (await page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 2000)"
            )) or ""
            if any(k in body for k in ("滑动", "拖动", "请完成验证", "拼图")):
                await _dump_state_and_screenshot(page, "slider_captcha")
                logger.error(
                    "PDD pushed a slider captcha — cannot auto-solve. "
                    "Screenshot saved. Fall back to method B "
                    "(local login + cookie export)."
                )
                return 4

            await _dump_state_and_screenshot(page, "after_send")
            logger.info(
                "Looks OK. SMS should arrive shortly. "
                "Next run: python scripts/pdd_login.py verify "
                f"--mobile {mobile} --otp <6_digit_code>"
            )
            return 0
        finally:
            await ctx.close()


async def phase_verify(mobile: str, otp: str) -> int:
    """Reopen the same profile, fill OTP, click login, export cookies
    into the accounts table on success.
    """
    from playwright.async_api import async_playwright

    if not PROFILE_DIR.exists():
        logger.error(
            f"{PROFILE_DIR} does not exist — run 'send' phase first."
        )
        return 10

    async with async_playwright() as p:
        ctx = await _launch(p)
        success = False
        try:
            page = await ctx.new_page()
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(3)

            # After the browser restart PDD may bounce us back to the
            # landing page even with the same profile; re-enter the SMS
            # form if that happened.
            try:
                await page.evaluate("""
() => {
  const box = document.querySelector('.login-box');
  if (!box) return;
  for (const c of box.children) {
    const t = (c.innerText || '').trim();
    if (t.includes('手机')) { c.click(); return; }
  }
  if (box.children.length >= 2) box.children[1].click();
}""")
                await asyncio.sleep(2)
            except Exception:
                pass

            # Re-locate OTP input. After the send phase closed the browser,
            # the UI may have reset to the pristine form, so we re-fill the
            # mobile first just in case.
            mobile_input = await _find_mobile_input(page)
            if mobile_input:
                try:
                    await mobile_input.fill(mobile)
                except Exception:
                    pass

            otp_input = await _find_otp_input(page)
            if not otp_input:
                await _dump_state_and_screenshot(page, "no_otp_input")
                logger.error("OTP input field not found.")
                return 11
            await otp_input.fill(otp)
            logger.info(f"filled OTP {otp}")
            await asyncio.sleep(1)

            clicked = await page.evaluate("""
() => {
  const form = document.querySelector('form.login-ui-form') || document.querySelector('.container');
  if (!form) return false;
  for (const b of form.querySelectorAll('button')) {
    const t = (b.innerText || '').trim();
    if (t === '登录' || t.includes('确认登录') || t.includes('立即登录')) {
      if (b.disabled) return 'disabled';
      b.click();
      return true;
    }
  }
  return false;
}
""")
            if clicked != True:
                await _dump_state_and_screenshot(page, "no_login_btn")
                logger.error(f"login button not clickable (state={clicked}).")
                return 12

            # Wait for navigation to a post-login page (home / profile / search).
            try:
                await page.wait_for_url(
                    lambda u: "login" not in u, timeout=15_000
                )
            except Exception:
                logger.warning("URL did not change after login click")

            await asyncio.sleep(3)
            await _dump_state_and_screenshot(page, "after_login")

            # Heuristic: did we actually log in?
            body_text = (await page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 1500)"
            )) or ""
            storage_state = await ctx.storage_state()
            has_auth_cookie = any(
                c.get("name") in ("PDDAccessToken", "pdd_user_id", "pdd_user_uin", "PASS_ID")
                for c in storage_state.get("cookies", [])
            )
            login_keywords_blocking = any(
                k in body_text for k in ("验证码错误", "请输入验证码", "登录失败", "拼图")
            )
            if not has_auth_cookie or login_keywords_blocking:
                logger.error(
                    f"login verification failed: "
                    f"has_auth_cookie={has_auth_cookie}, body_snippet={body_text[:200]}"
                )
                return 13

            state_json = json.dumps(storage_state, ensure_ascii=False)
            logger.info(
                f"login OK. Captured {len(storage_state.get('cookies', []))} cookies."
            )

            # Write to accounts table under the crawler convention.
            await _persist_account(mobile, state_json)
            success = True
            return 0
        finally:
            if not success:
                logger.warning(
                    "Keeping profile dir for inspection. If you want to "
                    "retry from scratch, rm -rf /tmp/pdd_login_profile"
                )
            await ctx.close()


async def _persist_account(mobile: str, state_json: str) -> None:
    """Insert/update the pdd_crawler account row. Uses asyncpg directly
    to avoid importing the whole app on the CLI path.
    """
    import asyncpg
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.core.config import get_settings

    settings = get_settings()
    raw_url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    account_name = f"pdd_crawler_{mobile[-4:]}"

    conn = await asyncpg.connect(raw_url)
    try:
        existing = await conn.fetchrow(
            "SELECT id FROM accounts WHERE account_name = $1 AND platform = 'pdd_crawler'",
            account_name,
        )
        if existing:
            await conn.execute(
                "UPDATE accounts SET cookies_data = $1, is_active = true, "
                "session_status = 'active', updated_at = NOW() WHERE id = $2",
                state_json, existing["id"],
            )
            logger.info(f"updated existing pdd_crawler account '{account_name}'")
        else:
            # identity_group='crawler' flags this as part of the short-term
            # IP pool; lifecycle_stage='nurturing' matches the enum used
            # elsewhere for "operational but not yet trusted" accounts.
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
            logger.info(f"inserted new pdd_crawler account '{account_name}'")
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="PDD crawler login helper")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s_send = sub.add_parser("send", help="Request the OTP SMS")
    s_send.add_argument("--mobile", required=True)
    s_verify = sub.add_parser("verify", help="Submit OTP and store cookies")
    s_verify.add_argument("--mobile", required=True)
    s_verify.add_argument("--otp", required=True)
    args = parser.parse_args()

    if args.cmd == "send":
        sys.exit(asyncio.run(phase_send(args.mobile)))
    elif args.cmd == "verify":
        sys.exit(asyncio.run(phase_verify(args.mobile, args.otp)))


if __name__ == "__main__":
    main()
