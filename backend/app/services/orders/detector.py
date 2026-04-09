"""
Order detection service.
Polls platform order pages via Playwright to detect new orders.
"""
import asyncio
import re
from datetime import datetime, timezone

from loguru import logger

from app.services.browser import browser_manager

XIANYU_ORDER_URL = "https://www.goofish.com/personal?tab=sold"
CHECK_INTERVAL_SEC = 180  # 3 minutes


async def _human_delay(min_s: float = 0.5, max_s: float = 2.0):
    import random
    await asyncio.sleep(random.uniform(min_s, max_s))


async def detect_xianyu_orders(
    account_id: str,
    account_config: dict,
    known_order_ids: set[str],
) -> list[dict]:
    """
    Check for new orders on Xianyu by polling the sold items page.

    Returns list of new orders with:
        sale_order_id, buyer_name, buyer_address, buyer_phone, buyer_note,
        sale_price, item_title, sku_info
    """
    context = await browser_manager.get_context(account_id, account_config)
    page = await context.new_page()

    try:
        await page.goto(XIANYU_ORDER_URL, wait_until="networkidle", timeout=30000)
        await _human_delay(2, 4)

        current_url = page.url
        if "login" in current_url.lower():
            logger.warning(f"Account {account_id} needs re-login for order check")
            return []

        # Wait for order list to load
        await page.wait_for_selector('[class*="order"], [class*="trade"]', timeout=10000)
        await _human_delay(1, 2)

        new_orders = []

        # Parse order cards
        order_cards = await page.query_selector_all('[class*="order-card"], [class*="trade-item"], .order-item')

        for card in order_cards:
            try:
                order_id_el = await card.query_selector('[class*="order-id"], [class*="trade-no"]')
                order_id = (await order_id_el.inner_text()).strip() if order_id_el else None

                if not order_id or order_id in known_order_ids:
                    continue

                status_el = await card.query_selector('[class*="status"]')
                status_text = (await status_el.inner_text()).strip() if status_el else ""

                if "待发货" not in status_text and "已付款" not in status_text:
                    continue

                title_el = await card.query_selector('[class*="title"], [class*="item-name"]')
                item_title = (await title_el.inner_text()).strip() if title_el else ""

                price_el = await card.query_selector('[class*="price"]')
                price_text = (await price_el.inner_text()).strip() if price_el else "0"
                price = float(re.sub(r'[^\d.]', '', price_text) or 0)

                # Click into order detail for buyer info
                detail_link = await card.query_selector('a[href*="detail"], [class*="detail"]')
                if detail_link:
                    detail_page = await context.new_page()
                    detail_url = await detail_link.get_attribute("href")
                    if detail_url:
                        if not detail_url.startswith("http"):
                            detail_url = f"https://www.goofish.com{detail_url}"
                        await detail_page.goto(detail_url, wait_until="networkidle", timeout=20000)
                        await _human_delay(1, 3)

                        buyer_info = await _extract_buyer_info(detail_page)
                        await detail_page.close()
                    else:
                        buyer_info = {}
                else:
                    buyer_info = {}

                new_orders.append({
                    "sale_order_id": order_id,
                    "item_title": item_title,
                    "sale_price": price,
                    "buyer_name": buyer_info.get("name", ""),
                    "buyer_address": buyer_info.get("address", ""),
                    "buyer_phone": buyer_info.get("phone", ""),
                    "buyer_note": buyer_info.get("note", ""),
                    "sku_info": buyer_info.get("sku", {}),
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                })
                logger.info(f"New order detected: {order_id} - {item_title} ¥{price}")

            except Exception as e:
                logger.debug(f"Failed to parse order card: {e}")
                continue

        await browser_manager.save_state(account_id)
        return new_orders

    except Exception as e:
        logger.error(f"Order detection failed for {account_id}: {e}")
        return []
    finally:
        await page.close()


async def _extract_buyer_info(page) -> dict:
    """Extract buyer name, address, phone from order detail page."""
    info = {}
    try:
        addr_selectors = [
            '[class*="address"]',
            '[class*="receiver"]',
            '[class*="consignee"]',
        ]
        for sel in addr_selectors:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                info["address"] = text.strip()
                name_match = re.search(r'收货人[：:]?\s*(.+?)[\s\n]', text)
                phone_match = re.search(r'(\d{11})', text)
                if name_match:
                    info["name"] = name_match.group(1).strip()
                if phone_match:
                    info["phone"] = phone_match.group(1)
                break

        note_el = await page.query_selector('[class*="remark"], [class*="note"], [class*="memo"]')
        if note_el:
            info["note"] = (await note_el.inner_text()).strip()

        sku_el = await page.query_selector('[class*="sku"], [class*="spec"]')
        if sku_el:
            info["sku"] = {"text": (await sku_el.inner_text()).strip()}

    except Exception as e:
        logger.debug(f"Buyer info extraction error: {e}")

    return info
