"""
Xianyu Playwright-based publishing engine.
Handles automated listing publication with anti-detection measures.
"""
import asyncio
import random
from datetime import datetime, timezone

from loguru import logger

from app.services.browser import browser_manager

SAFE_PUBLISH_WINDOWS = [
    (8, 9), (12, 13.5), (18, 22),
]

MIN_PUBLISH_INTERVAL_SEC = 30 * 60  # 30 minutes
MAX_PUBLISH_INTERVAL_SEC = 3 * 60 * 60  # 3 hours


def is_safe_time() -> bool:
    """Check if current time is within safe publishing windows."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    hour = (now.hour + 8) % 24  # UTC -> CST
    minute_frac = hour + now.minute / 60
    return any(start <= minute_frac < end for start, end in SAFE_PUBLISH_WINDOWS)


async def _human_delay(min_sec: float = 0.5, max_sec: float = 2.5):
    await asyncio.sleep(random.uniform(min_sec, max_sec))


async def _type_slowly(page, selector: str, text: str):
    """Type text character by character with random delays."""
    element = await page.query_selector(selector)
    if not element:
        logger.error(f"Element not found: {selector}")
        return
    await element.click()
    await _human_delay(0.3, 0.8)
    for char in text:
        await page.keyboard.type(char, delay=random.randint(50, 200))
        if random.random() < 0.05:
            await _human_delay(0.5, 1.5)


async def _random_scroll(page, direction: str = "down"):
    """Simulate human-like scrolling."""
    delta = random.randint(100, 400) * (1 if direction == "down" else -1)
    await page.mouse.wheel(0, delta)
    await _human_delay(0.3, 1.0)


async def publish_listing(
    account_id: str,
    account_config: dict,
    listing_data: dict,
) -> dict:
    """
    Publish a listing to Xianyu via Playwright.

    listing_data keys:
        title: str
        description: str
        price: float
        image_paths: list[str]
        category: str (optional)

    Returns:
        {success: bool, xianyu_item_id: str|None, error: str|None}
    """
    context = await browser_manager.get_context(account_id, account_config)
    page = await context.new_page()

    try:
        # Navigate to publish page
        await page.goto("https://www.goofish.com/publish", wait_until="networkidle", timeout=30000)
        await _human_delay(2, 4)

        current_url = page.url
        if "login" in current_url.lower() or "sign" in current_url.lower():
            logger.warning(f"Account {account_id} needs re-login")
            return {"success": False, "xianyu_item_id": None, "error": "需要重新登录"}

        # Upload images
        image_paths = listing_data.get("image_paths", [])
        if image_paths:
            file_input = await page.query_selector('input[type="file"]')
            if file_input:
                for img_path in image_paths[:9]:
                    await file_input.set_input_files(img_path)
                    await _human_delay(1, 3)
                logger.info(f"Uploaded {len(image_paths[:9])} images")

        await _human_delay(1, 2)
        await _random_scroll(page)

        # Fill title
        title_selectors = [
            'textarea[placeholder*="标题"]',
            'textarea[placeholder*="宝贝"]',
            'input[placeholder*="标题"]',
            '#title',
            '.title-input textarea',
        ]
        for sel in title_selectors:
            element = await page.query_selector(sel)
            if element:
                await _type_slowly(page, sel, listing_data["title"])
                break
        else:
            logger.warning("Title input not found, trying fallback")

        await _human_delay(1, 2)

        # Fill description
        desc_selectors = [
            'textarea[placeholder*="描述"]',
            'textarea[placeholder*="详细"]',
            '.desc-input textarea',
            '#description',
        ]
        for sel in desc_selectors:
            element = await page.query_selector(sel)
            if element:
                await _type_slowly(page, sel, listing_data["description"])
                break

        await _human_delay(1, 2)
        await _random_scroll(page)

        # Fill price
        price_selectors = [
            'input[placeholder*="价格"]',
            'input[placeholder*="¥"]',
            '.price-input input',
            '#price',
        ]
        for sel in price_selectors:
            element = await page.query_selector(sel)
            if element:
                await element.click()
                await _human_delay(0.3, 0.5)
                await page.keyboard.type(str(listing_data["price"]), delay=random.randint(80, 180))
                break

        await _human_delay(2, 4)
        await _random_scroll(page, "down")

        # Click publish button
        publish_selectors = [
            'button:has-text("发布")',
            'button:has-text("确认发布")',
            '.publish-btn',
            'button[type="submit"]',
        ]
        for sel in publish_selectors:
            btn = await page.query_selector(sel)
            if btn:
                await _human_delay(1, 2)
                await btn.click()
                logger.info("Clicked publish button")
                break

        # Wait for result
        await _human_delay(3, 6)

        # Check if published successfully
        final_url = page.url
        if "success" in final_url.lower() or "detail" in final_url.lower():
            # Try to extract item ID from URL
            import re
            match = re.search(r'id=(\d+)', final_url)
            xianyu_id = match.group(1) if match else None
            logger.info(f"Published successfully: {xianyu_id}")
            await browser_manager.save_state(account_id)
            return {"success": True, "xianyu_item_id": xianyu_id, "error": None}

        # Check for error messages
        error_el = await page.query_selector('.error-message, .toast-error, [class*="error"]')
        error_text = await error_el.inner_text() if error_el else "发布状态未知"

        return {"success": False, "xianyu_item_id": None, "error": error_text}

    except Exception as e:
        logger.error(f"Publish failed for account {account_id}: {e}")
        return {"success": False, "xianyu_item_id": None, "error": str(e)}
    finally:
        await page.close()


async def refresh_listing(
    account_id: str,
    account_config: dict,
    xianyu_item_id: str,
) -> dict:
    """Refresh (擦亮) a listing on Xianyu."""
    context = await browser_manager.get_context(account_id, account_config)
    page = await context.new_page()

    try:
        await page.goto(
            f"https://www.goofish.com/personal?tab=sell",
            wait_until="networkidle",
            timeout=30000,
        )
        await _human_delay(2, 4)

        # Find the listing and click refresh
        item_card = await page.query_selector(f'[data-id="{xianyu_item_id}"]')
        if item_card:
            refresh_btn = await item_card.query_selector('button:has-text("擦亮"), [class*="refresh"]')
            if refresh_btn:
                await _human_delay(0.5, 1.5)
                await refresh_btn.click()
                await _human_delay(2, 3)
                await browser_manager.save_state(account_id)
                return {"success": True, "error": None}

        return {"success": False, "error": "未找到该商品或擦亮按钮"}

    except Exception as e:
        logger.error(f"Refresh failed: {e}")
        return {"success": False, "error": str(e)}
    finally:
        await page.close()


async def update_listing_price(
    account_id: str,
    account_config: dict,
    xianyu_item_id: str,
    new_price: float,
) -> dict:
    """Update the price of an existing listing."""
    context = await browser_manager.get_context(account_id, account_config)
    page = await context.new_page()

    try:
        await page.goto(
            f"https://www.goofish.com/edit?id={xianyu_item_id}",
            wait_until="networkidle",
            timeout=30000,
        )
        await _human_delay(2, 4)

        price_input = await page.query_selector('input[placeholder*="价格"], input[placeholder*="¥"], #price')
        if price_input:
            await price_input.click(click_count=3)
            await _human_delay(0.3, 0.5)
            await page.keyboard.type(str(new_price), delay=random.randint(80, 150))
            await _human_delay(1, 2)

            save_btn = await page.query_selector('button:has-text("保存"), button:has-text("确认")')
            if save_btn:
                await save_btn.click()
                await _human_delay(2, 4)
                await browser_manager.save_state(account_id)
                return {"success": True, "error": None}

        return {"success": False, "error": "未找到价格输入框"}
    except Exception as e:
        logger.error(f"Price update failed: {e}")
        return {"success": False, "error": str(e)}
    finally:
        await page.close()


async def remove_listing(
    account_id: str,
    account_config: dict,
    xianyu_item_id: str,
) -> dict:
    """Remove (下架) a listing from Xianyu."""
    context = await browser_manager.get_context(account_id, account_config)
    page = await context.new_page()

    try:
        await page.goto(
            f"https://www.goofish.com/personal?tab=sell",
            wait_until="networkidle",
            timeout=30000,
        )
        await _human_delay(2, 4)

        item_card = await page.query_selector(f'[data-id="{xianyu_item_id}"]')
        if item_card:
            remove_btn = await item_card.query_selector('button:has-text("下架"), button:has-text("删除")')
            if remove_btn:
                await _human_delay(0.5, 1)
                await remove_btn.click()
                await _human_delay(1, 2)

                confirm_btn = await page.query_selector('button:has-text("确认"), button:has-text("确定")')
                if confirm_btn:
                    await confirm_btn.click()
                    await _human_delay(2, 3)
                    await browser_manager.save_state(account_id)
                    return {"success": True, "error": None}

        return {"success": False, "error": "未找到商品或下架按钮"}
    except Exception as e:
        logger.error(f"Remove failed: {e}")
        return {"success": False, "error": str(e)}
    finally:
        await page.close()
