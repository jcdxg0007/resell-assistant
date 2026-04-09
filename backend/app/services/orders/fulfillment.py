"""
Order fulfillment service.
Handles source platform ordering, payment, and logistics sync.
"""
import asyncio
import random
import re
from datetime import datetime, timezone, timedelta

from loguru import logger

from app.core.config import get_settings
from app.services.browser import browser_manager

settings = get_settings()

PDD_ORDER_URL = "https://mobile.yangkeduo.com/order.html"
TAOBAO_ORDER_URL = "https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm"


async def _human_delay(min_s: float = 0.5, max_s: float = 2.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _type_slowly(page, selector: str, text: str):
    el = await page.query_selector(selector)
    if not el:
        return False
    await el.click()
    await _human_delay(0.3, 0.6)
    for char in text:
        await page.keyboard.type(char, delay=random.randint(60, 180))
    return True


class FulfillmentService:

    def __init__(self):
        self._daily_spend: dict[str, float] = {}  # date_str -> total spend
        self._daily_count: dict[str, int] = {}

    def _check_payment_limit(self, amount: float) -> tuple[bool, str]:
        """Check if auto-payment is within daily limits."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        spent = self._daily_spend.get(today, 0)
        count = self._daily_count.get(today, 0)

        if amount > settings.AUTO_PAY_MAX_AMOUNT:
            return False, f"单笔¥{amount}超限(上限¥{settings.AUTO_PAY_MAX_AMOUNT})"

        if spent + amount > settings.AUTO_PAY_DAILY_LIMIT:
            return False, f"日累计¥{spent + amount}将超限(上限¥{settings.AUTO_PAY_DAILY_LIMIT})"

        return True, "ok"

    def _record_payment(self, amount: float):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._daily_spend[today] = self._daily_spend.get(today, 0) + amount
        self._daily_count[today] = self._daily_count.get(today, 0) + 1

    async def auto_purchase_pdd(
        self,
        account_id: str,
        account_config: dict,
        source_url: str,
        buyer_address: dict,
        sku_mapping: dict | None = None,
        expected_price: float = 0,
    ) -> dict:
        """
        Auto-purchase on PDD with Alipay small-amount password-free payment.

        buyer_address: {name, phone, province, city, district, detail}
        Returns: {success, source_order_id, actual_price, error}
        """
        # Safety check
        can_pay, reason = self._check_payment_limit(expected_price)
        if not can_pay:
            return {"success": False, "source_order_id": None, "actual_price": 0, "error": reason, "needs_manual": True}

        context = await browser_manager.get_context(account_id, account_config)
        page = await context.new_page()

        try:
            await page.goto(source_url, wait_until="networkidle", timeout=30000)
            await _human_delay(2, 4)

            # Check login status
            if "login" in page.url.lower():
                return {"success": False, "source_order_id": None, "actual_price": 0,
                        "error": "拼多多需要重新登录", "needs_manual": True}

            # Check stock
            soldout = await page.query_selector('[class*="sold-out"], [class*="off-shelf"]')
            if soldout:
                return {"success": False, "source_order_id": None, "actual_price": 0,
                        "error": "源商品缺货/下架", "needs_manual": True}

            # Select SKU if needed
            if sku_mapping:
                for attr, value in sku_mapping.items():
                    sku_btn = await page.query_selector(f'[class*="sku"] button:has-text("{value}")')
                    if sku_btn:
                        await sku_btn.click()
                        await _human_delay(0.5, 1)

            # Click buy now
            buy_btn = await page.query_selector('button:has-text("立即购买"), button:has-text("立即抢购")')
            if not buy_btn:
                buy_btn = await page.query_selector('button:has-text("确认")')
            if buy_btn:
                await _human_delay(0.5, 1.5)
                await buy_btn.click()
                await _human_delay(2, 4)

            # Fill address on order confirmation page
            await self._fill_address_pdd(page, buyer_address)
            await _human_delay(1, 2)

            # Verify price before payment
            actual_price = await self._extract_price(page)
            if actual_price and expected_price > 0:
                price_diff = abs(actual_price - expected_price) / expected_price
                if price_diff > 0.10:
                    return {"success": False, "source_order_id": None, "actual_price": actual_price,
                            "error": f"价格异常: 预期¥{expected_price} 实际¥{actual_price} (差异{price_diff*100:.1f}%)",
                            "needs_manual": True}

            # Re-check payment limit with actual price
            if actual_price:
                can_pay, reason = self._check_payment_limit(actual_price)
                if not can_pay:
                    return {"success": False, "source_order_id": None, "actual_price": actual_price,
                            "error": reason, "needs_manual": True}

            # Submit order (Alipay small-amount password-free)
            submit_btn = await page.query_selector('button:has-text("提交订单"), button:has-text("确认支付")')
            if submit_btn:
                await _human_delay(1, 2)
                await submit_btn.click()
                await _human_delay(3, 6)

            # Check for payment confirmation (password-free should auto-complete)
            await page.wait_for_timeout(5000)

            # Extract order ID from success page
            source_order_id = await self._extract_source_order_id(page)

            if source_order_id:
                self._record_payment(actual_price or expected_price)
                await browser_manager.save_state(account_id)
                logger.info(f"PDD purchase success: order {source_order_id}, ¥{actual_price}")
                return {
                    "success": True,
                    "source_order_id": source_order_id,
                    "actual_price": actual_price or expected_price,
                    "error": None,
                    "needs_manual": False,
                }

            return {"success": False, "source_order_id": None, "actual_price": actual_price,
                    "error": "下单结果未确认，可能需要手动输入支付密码", "needs_manual": True}

        except Exception as e:
            logger.error(f"PDD purchase failed: {e}")
            return {"success": False, "source_order_id": None, "actual_price": 0,
                    "error": str(e), "needs_manual": True}
        finally:
            await page.close()

    async def auto_purchase_taobao(
        self,
        account_id: str,
        account_config: dict,
        source_url: str,
        buyer_address: dict,
        sku_mapping: dict | None = None,
        expected_price: float = 0,
    ) -> dict:
        """Auto-purchase on Taobao (similar flow to PDD)."""
        can_pay, reason = self._check_payment_limit(expected_price)
        if not can_pay:
            return {"success": False, "source_order_id": None, "actual_price": 0,
                    "error": reason, "needs_manual": True}

        context = await browser_manager.get_context(account_id, account_config)
        page = await context.new_page()

        try:
            await page.goto(source_url, wait_until="networkidle", timeout=30000)
            await _human_delay(2, 4)

            if "login" in page.url.lower():
                return {"success": False, "source_order_id": None, "actual_price": 0,
                        "error": "淘宝需要重新登录", "needs_manual": True}

            # Select SKU
            if sku_mapping:
                for attr, value in sku_mapping.items():
                    sku_btn = await page.query_selector(f'[class*="sku"] button:has-text("{value}")')
                    if sku_btn:
                        await sku_btn.click()
                        await _human_delay(0.5, 1)

            # Click buy
            buy_btn = await page.query_selector('button:has-text("立即购买"), #J_LinkBuy')
            if buy_btn:
                await _human_delay(0.5, 1.5)
                await buy_btn.click()
                await _human_delay(2, 4)

            await self._fill_address_taobao(page, buyer_address)
            await _human_delay(1, 2)

            actual_price = await self._extract_price(page)
            if actual_price and expected_price > 0:
                price_diff = abs(actual_price - expected_price) / expected_price
                if price_diff > 0.10:
                    return {"success": False, "source_order_id": None, "actual_price": actual_price,
                            "error": f"价格异常: 预期¥{expected_price} 实际¥{actual_price}",
                            "needs_manual": True}

            submit_btn = await page.query_selector('button:has-text("提交订单")')
            if submit_btn:
                await _human_delay(1, 2)
                await submit_btn.click()
                await _human_delay(3, 6)

            await page.wait_for_timeout(5000)
            source_order_id = await self._extract_source_order_id(page)

            if source_order_id:
                self._record_payment(actual_price or expected_price)
                await browser_manager.save_state(account_id)
                return {"success": True, "source_order_id": source_order_id,
                        "actual_price": actual_price or expected_price, "error": None, "needs_manual": False}

            return {"success": False, "source_order_id": None, "actual_price": actual_price,
                    "error": "下单结果未确认", "needs_manual": True}

        except Exception as e:
            logger.error(f"Taobao purchase failed: {e}")
            return {"success": False, "source_order_id": None, "actual_price": 0,
                    "error": str(e), "needs_manual": True}
        finally:
            await page.close()

    async def _fill_address_pdd(self, page, address: dict):
        """Fill buyer address on PDD order page."""
        # Check if there's an existing address that needs updating
        change_btn = await page.query_selector('button:has-text("修改地址"), [class*="address-edit"]')
        if change_btn:
            await change_btn.click()
            await _human_delay(1, 2)

        fields = [
            ('input[placeholder*="姓名"], input[placeholder*="收货人"]', address.get("name", "")),
            ('input[placeholder*="手机"], input[placeholder*="电话"]', address.get("phone", "")),
        ]
        for sel, val in fields:
            if val:
                await _type_slowly(page, sel, val)
                await _human_delay(0.3, 0.8)

        # Region selection (province/city/district) via dropdown
        region_selectors = ['[class*="region"], [class*="area"], [class*="address-picker"]']
        for sel in region_selectors:
            region_el = await page.query_selector(sel)
            if region_el:
                await region_el.click()
                await _human_delay(0.5, 1)
                for level in ["province", "city", "district"]:
                    val = address.get(level, "")
                    if val:
                        option = await page.query_selector(f'[class*="option"]:has-text("{val}")')
                        if option:
                            await option.click()
                            await _human_delay(0.3, 0.6)
                break

        detail_addr = address.get("detail", "")
        if detail_addr:
            await _type_slowly(page, 'input[placeholder*="详细地址"], textarea[placeholder*="详细地址"]', detail_addr)

        save_btn = await page.query_selector('button:has-text("保存"), button:has-text("确认")')
        if save_btn:
            await save_btn.click()
            await _human_delay(1, 2)

    async def _fill_address_taobao(self, page, address: dict):
        """Fill buyer address on Taobao order page."""
        change_btn = await page.query_selector('[class*="address"] button:has-text("修改"), [class*="address-edit"]')
        if change_btn:
            await change_btn.click()
            await _human_delay(1, 2)

        fields = [
            ('input[name*="name"], input[placeholder*="姓名"]', address.get("name", "")),
            ('input[name*="phone"], input[placeholder*="手机"]', address.get("phone", "")),
        ]
        for sel, val in fields:
            if val:
                el = await page.query_selector(sel)
                if el:
                    await el.click(click_count=3)
                    await _human_delay(0.2, 0.4)
                    await page.keyboard.type(val, delay=random.randint(60, 150))

        detail_addr = address.get("detail", "")
        if detail_addr:
            el = await page.query_selector('textarea[name*="address"], input[placeholder*="详细"]')
            if el:
                await el.click(click_count=3)
                await _human_delay(0.2, 0.4)
                await page.keyboard.type(detail_addr, delay=random.randint(60, 150))

        save_btn = await page.query_selector('button:has-text("保存")')
        if save_btn:
            await save_btn.click()
            await _human_delay(1, 2)

    async def _extract_price(self, page) -> float | None:
        """Extract total price from order confirmation page."""
        selectors = [
            '[class*="total-price"]',
            '[class*="pay-amount"]',
            '[class*="real-pay"]',
            '[class*="actual"]',
        ]
        for sel in selectors:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                match = re.search(r'[\d.]+', text)
                if match:
                    return float(match.group())
        return None

    async def _extract_source_order_id(self, page) -> str | None:
        """Extract source order ID from success/confirmation page."""
        text = await page.inner_text("body")
        patterns = [
            r'订单号[：:]\s*(\d{10,20})',
            r'order[_-]?(?:id|no)[：:=]\s*(\d{10,20})',
            r'(\d{15,20})',
        ]
        for p in patterns:
            match = re.search(p, text, re.IGNORECASE)
            if match:
                return match.group(1)
        return None


fulfillment_service = FulfillmentService()
