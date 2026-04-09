"""
Logistics tracking and sync service.
Tracks source platform shipments and syncs to sale platform.
"""
import asyncio
import random
import re
from datetime import datetime, timezone

from loguru import logger

from app.services.browser import browser_manager


async def _human_delay(min_s: float = 0.5, max_s: float = 2.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def check_source_shipment(
    account_id: str,
    account_config: dict,
    source_platform: str,
    source_order_id: str,
) -> dict:
    """
    Check shipment status on source platform.

    Returns: {shipped, carrier, tracking_number, events, error}
    """
    context = await browser_manager.get_context(account_id, account_config)
    page = await context.new_page()

    try:
        if source_platform == "pinduoduo":
            url = f"https://mobile.yangkeduo.com/order.html?order_sn={source_order_id}"
        elif source_platform == "taobao":
            url = f"https://buyertrade.taobao.com/trade/detail/trade_order_detail.htm?biz_order_id={source_order_id}"
        else:
            return {"shipped": False, "error": f"不支持的源平台: {source_platform}"}

        await page.goto(url, wait_until="networkidle", timeout=30000)
        await _human_delay(2, 4)

        # Look for tracking info
        tracking_el = await page.query_selector(
            '[class*="logistics"], [class*="tracking"], [class*="express"]'
        )

        if not tracking_el:
            return {"shipped": False, "carrier": None, "tracking_number": None, "events": [], "error": None}

        text = await tracking_el.inner_text()

        # Extract carrier and tracking number
        carrier = None
        tracking_number = None

        carrier_patterns = [
            r'(顺丰|中通|圆通|韵达|申通|百世|极兔|邮政|EMS|德邦|京东物流)',
            r'carrier[：:]\s*(.+?)[\s\n]',
        ]
        for p in carrier_patterns:
            m = re.search(p, text)
            if m:
                carrier = m.group(1).strip()
                break

        tracking_patterns = [
            r'(SF\d{12,15})',
            r'(YT\d{13,15})',
            r'(JT\d{13,15})',
            r'(\d{12,15})',
        ]
        for p in tracking_patterns:
            m = re.search(p, text)
            if m:
                tracking_number = m.group(1)
                break

        # Extract tracking events
        events = []
        event_els = await page.query_selector_all(
            '[class*="logistics-item"], [class*="track-item"], [class*="event"]'
        )
        for ev_el in event_els[:10]:
            try:
                ev_text = await ev_el.inner_text()
                events.append(ev_text.strip())
            except Exception:
                pass

        shipped = bool(tracking_number)
        await browser_manager.save_state(account_id)
        return {
            "shipped": shipped,
            "carrier": carrier,
            "tracking_number": tracking_number,
            "events": events,
            "error": None,
        }

    except Exception as e:
        logger.error(f"Shipment check failed: {e}")
        return {"shipped": False, "carrier": None, "tracking_number": None, "events": [], "error": str(e)}
    finally:
        await page.close()


async def sync_tracking_to_xianyu(
    account_id: str,
    account_config: dict,
    sale_order_id: str,
    carrier: str,
    tracking_number: str,
) -> dict:
    """
    Sync tracking information to Xianyu order.
    Fill in the shipping details via Playwright.
    """
    context = await browser_manager.get_context(account_id, account_config)
    page = await context.new_page()

    try:
        # Navigate to sold orders page
        await page.goto(
            "https://www.goofish.com/personal?tab=sold",
            wait_until="networkidle",
            timeout=30000,
        )
        await _human_delay(2, 4)

        # Find the specific order
        order_el = await page.query_selector(f'[data-order-id="{sale_order_id}"]')
        if not order_el:
            order_cards = await page.query_selector_all('[class*="order-card"]')
            for card in order_cards:
                text = await card.inner_text()
                if sale_order_id in text:
                    order_el = card
                    break

        if not order_el:
            return {"success": False, "error": "未找到对应订单"}

        # Click "发货" button
        ship_btn = await order_el.query_selector('button:has-text("发货"), button:has-text("填写物流")')
        if not ship_btn:
            return {"success": False, "error": "未找到发货按钮，可能已发货"}

        await ship_btn.click()
        await _human_delay(1, 3)

        # Fill carrier
        carrier_input = await page.query_selector(
            'input[placeholder*="快递"], input[placeholder*="物流"], select[class*="carrier"]'
        )
        if carrier_input:
            tag = await carrier_input.evaluate("el => el.tagName")
            if tag.lower() == "select":
                await page.select_option(carrier_input, label=carrier)
            else:
                await carrier_input.click(click_count=3)
                await _human_delay(0.3, 0.5)
                await page.keyboard.type(carrier, delay=random.randint(80, 150))
                await _human_delay(0.5, 1)
                suggestion = await page.query_selector(f'[class*="suggestion"]:has-text("{carrier}")')
                if suggestion:
                    await suggestion.click()

        await _human_delay(0.5, 1)

        # Fill tracking number
        tracking_input = await page.query_selector(
            'input[placeholder*="单号"], input[placeholder*="运单"]'
        )
        if tracking_input:
            await tracking_input.click()
            await _human_delay(0.3, 0.5)
            await page.keyboard.type(tracking_number, delay=random.randint(60, 150))

        await _human_delay(1, 2)

        # Submit
        confirm_btn = await page.query_selector('button:has-text("确认发货"), button:has-text("提交")')
        if confirm_btn:
            await confirm_btn.click()
            await _human_delay(2, 4)

        await browser_manager.save_state(account_id)
        logger.info(f"Tracking synced to Xianyu: order={sale_order_id}, {carrier} {tracking_number}")
        return {"success": True, "error": None}

    except Exception as e:
        logger.error(f"Tracking sync failed: {e}")
        return {"success": False, "error": str(e)}
    finally:
        await page.close()


async def get_source_return_address(
    account_id: str,
    account_config: dict,
    source_platform: str,
    source_order_id: str,
) -> dict:
    """
    Get the return address from source platform merchant.
    Used when buyer requests a return on Xianyu.

    Returns: {success, address, contact_name, contact_phone, error}
    """
    context = await browser_manager.get_context(account_id, account_config)
    page = await context.new_page()

    try:
        if source_platform == "pinduoduo":
            # Navigate to PDD order and initiate return
            url = f"https://mobile.yangkeduo.com/order.html?order_sn={source_order_id}"
        else:
            url = f"https://buyertrade.taobao.com/trade/detail/trade_order_detail.htm?biz_order_id={source_order_id}"

        await page.goto(url, wait_until="networkidle", timeout=30000)
        await _human_delay(2, 4)

        # Click return/refund button
        return_btn = await page.query_selector(
            'button:has-text("退货"), button:has-text("退款"), [class*="refund"]'
        )
        if return_btn:
            await return_btn.click()
            await _human_delay(2, 4)

        # Look for return address info
        addr_selectors = [
            '[class*="return-address"]',
            '[class*="refund-address"]',
            '[class*="merchant-address"]',
        ]
        for sel in addr_selectors:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                address_match = re.search(r'地址[：:]\s*(.+?)(?:\n|$)', text)
                name_match = re.search(r'(?:收货人|联系人)[：:]\s*(.+?)[\s\n]', text)
                phone_match = re.search(r'(\d{11})', text)

                return {
                    "success": True,
                    "address": address_match.group(1).strip() if address_match else text.strip(),
                    "contact_name": name_match.group(1).strip() if name_match else "",
                    "contact_phone": phone_match.group(1) if phone_match else "",
                    "error": None,
                }

        return {"success": False, "address": "", "contact_name": "", "contact_phone": "",
                "error": "未找到退货地址，可能需要等待商家审核"}

    except Exception as e:
        logger.error(f"Return address extraction failed: {e}")
        return {"success": False, "address": "", "contact_name": "", "contact_phone": "",
                "error": str(e)}
    finally:
        await page.close()
