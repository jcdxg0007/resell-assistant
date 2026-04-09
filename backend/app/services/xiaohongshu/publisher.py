"""
XHS note publishing service via Playwright.
Handles automated note publishing to Xiaohongshu Creator Center.
"""
import asyncio
import random
from datetime import datetime, timezone

from loguru import logger

from app.services.browser import browser_manager

XHS_CREATOR_URL = "https://creator.xiaohongshu.com/publish/publish"

SAFE_PUBLISH_HOURS = [(11, 13), (17, 19), (20, 22)]


def is_xhs_safe_time() -> bool:
    now = datetime.now(timezone.utc)
    hour = (now.hour + 8) % 24
    return any(s <= hour < e for s, e in SAFE_PUBLISH_HOURS)


async def _human_delay(min_s: float = 0.5, max_s: float = 2.5):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _type_slowly(page, selector: str, text: str, clear: bool = False):
    el = await page.query_selector(selector)
    if not el:
        return False
    await el.click()
    if clear:
        await page.keyboard.press("Meta+a")
        await _human_delay(0.2, 0.4)
        await page.keyboard.press("Backspace")
    await _human_delay(0.3, 0.6)
    for char in text:
        await page.keyboard.type(char, delay=random.randint(40, 160))
        if random.random() < 0.03:
            await _human_delay(0.3, 1.0)
    return True


async def publish_note(
    account_id: str,
    account_config: dict,
    note_data: dict,
) -> dict:
    """
    Publish a note to XHS via Creator Center.

    note_data:
        title: str
        body: str
        image_paths: list[str]
        tags: list[str]
        topics: list[str]
        content_type: "image" | "video"

    Returns: {success, xhs_note_id, error}
    """
    context = await browser_manager.get_context(account_id, account_config)
    page = await context.new_page()

    try:
        await page.goto(XHS_CREATOR_URL, wait_until="networkidle", timeout=30000)
        await _human_delay(2, 4)

        if "login" in page.url.lower():
            return {"success": False, "xhs_note_id": None, "error": "需要重新登录小红书创作者中心"}

        # Upload images
        image_paths = note_data.get("image_paths", [])
        if image_paths:
            upload_input = await page.query_selector('input[type="file"]')
            if upload_input:
                for path in image_paths[:9]:
                    await upload_input.set_input_files(path)
                    await _human_delay(1, 3)
                logger.info(f"Uploaded {len(image_paths[:9])} images to XHS")
            await _human_delay(2, 4)

        # Fill title
        title_selectors = [
            'input[placeholder*="标题"]',
            '#title',
            '[class*="title"] input',
            'input[maxlength="20"]',
        ]
        for sel in title_selectors:
            if await _type_slowly(page, sel, note_data["title"]):
                break
        await _human_delay(0.5, 1)

        # Fill body
        body_selectors = [
            '[contenteditable="true"]',
            'div[class*="editor"]',
            'div[class*="content"][contenteditable]',
            '#post-textarea',
        ]
        for sel in body_selectors:
            el = await page.query_selector(sel)
            if el:
                await el.click()
                await _human_delay(0.5, 1)
                # Type body in chunks to appear natural
                body = note_data["body"]
                chunk_size = random.randint(20, 50)
                for i in range(0, len(body), chunk_size):
                    chunk = body[i:i + chunk_size]
                    await page.keyboard.type(chunk, delay=random.randint(20, 80))
                    if random.random() < 0.15:
                        await _human_delay(0.5, 2)
                break
        await _human_delay(1, 2)

        # Add tags/topics
        for tag in (note_data.get("tags") or [])[:5]:
            tag_input = await page.query_selector(
                'input[placeholder*="标签"], input[placeholder*="话题"]'
            )
            if tag_input:
                await tag_input.click()
                await _human_delay(0.3, 0.5)
                await page.keyboard.type(f"#{tag}", delay=random.randint(60, 120))
                await _human_delay(0.5, 1)
                suggestion = await page.query_selector(f'[class*="suggest"]:has-text("{tag}")')
                if suggestion:
                    await suggestion.click()
                else:
                    await page.keyboard.press("Enter")
                await _human_delay(0.5, 1)

        await _human_delay(1, 2)

        # Click publish
        publish_selectors = [
            'button:has-text("发布")',
            'button:has-text("发布笔记")',
            '[class*="publish"] button',
        ]
        for sel in publish_selectors:
            btn = await page.query_selector(sel)
            if btn:
                await _human_delay(1, 2)
                await btn.click()
                break

        await _human_delay(3, 6)

        # Check result
        if "success" in page.url.lower() or "publish" not in page.url.lower():
            await browser_manager.save_state(account_id)
            import re
            match = re.search(r'noteId=([a-f0-9]+)', page.url)
            note_id = match.group(1) if match else None
            return {"success": True, "xhs_note_id": note_id, "error": None}

        error_el = await page.query_selector('[class*="error"], [class*="toast"]')
        error_text = (await error_el.inner_text()).strip() if error_el else "发布结果未确认"
        return {"success": False, "xhs_note_id": None, "error": error_text}

    except Exception as e:
        logger.error(f"XHS publish failed: {e}")
        return {"success": False, "xhs_note_id": None, "error": str(e)}
    finally:
        await page.close()
