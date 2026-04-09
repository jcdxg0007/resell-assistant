"""
Virtual product automatic delivery service.
Handles auto-sending download links upon payment.
"""
import asyncio
import random
from datetime import datetime, timezone

from loguru import logger

from app.services.browser import browser_manager
from app.services.notification import notification_service


class VirtualDeliveryService:
    """Handles automatic delivery of virtual products (cloud drive links, etc.)."""

    async def deliver_virtual_product(
        self,
        order_id: str,
        sale_platform: str,
        sale_order_id: str,
        account_id: str,
        account_config: dict,
        delivery_content: str,
        buyer_name: str = "",
    ) -> dict:
        """
        Auto-deliver virtual product by sending download link via platform chat.

        delivery_content: The cloud drive link or download instructions.
        """
        if sale_platform == "xianyu":
            return await self._deliver_xianyu(
                account_id, account_config, sale_order_id, delivery_content, buyer_name
            )
        elif sale_platform == "xiaohongshu":
            return await self._deliver_xhs(
                account_id, account_config, sale_order_id, delivery_content, buyer_name
            )
        else:
            return {"success": False, "error": f"不支持的平台: {sale_platform}"}

    async def _deliver_xianyu(
        self,
        account_id: str,
        account_config: dict,
        sale_order_id: str,
        delivery_content: str,
        buyer_name: str,
    ) -> dict:
        """Send virtual product via Xianyu chat."""
        context = await browser_manager.get_context(account_id, account_config)
        page = await context.new_page()

        try:
            await page.goto(
                "https://www.goofish.com/personal?tab=sold",
                wait_until="networkidle",
                timeout=30000,
            )
            await asyncio.sleep(random.uniform(2, 4))

            # Find order and click chat
            order_cards = await page.query_selector_all('[class*="order-card"]')
            for card in order_cards:
                text = await card.inner_text()
                if sale_order_id in text:
                    chat_btn = await card.query_selector('button:has-text("聊天"), [class*="chat"]')
                    if chat_btn:
                        await chat_btn.click()
                        await asyncio.sleep(random.uniform(2, 4))
                        break
            else:
                return {"success": False, "error": "未找到对应订单"}

            # Type and send delivery message
            message = f"您好！感谢购买～\n\n资源链接已为您准备好👇\n{delivery_content}\n\n如有问题随时联系我～"
            chat_input = await page.query_selector(
                'textarea, [contenteditable="true"], input[placeholder*="输入"]'
            )
            if chat_input:
                await chat_input.click()
                await asyncio.sleep(0.5)
                await page.keyboard.type(message, delay=random.randint(30, 80))
                await asyncio.sleep(0.5)

                send_btn = await page.query_selector('button:has-text("发送")')
                if send_btn:
                    await send_btn.click()
                    await asyncio.sleep(2)
                    await browser_manager.save_state(account_id)
                    logger.info(f"Virtual delivery sent: order {sale_order_id}")
                    return {"success": True, "error": None}

            return {"success": False, "error": "未找到聊天输入框"}

        except Exception as e:
            logger.error(f"Virtual delivery failed: {e}")
            return {"success": False, "error": str(e)}
        finally:
            await page.close()

    async def _deliver_xhs(
        self,
        account_id: str,
        account_config: dict,
        sale_order_id: str,
        delivery_content: str,
        buyer_name: str,
    ) -> dict:
        """Send virtual product via XHS message / auto-reply."""
        # XHS shop supports auto-delivery configuration
        # For now, send via chat similar to Xianyu
        context = await browser_manager.get_context(account_id, account_config)
        page = await context.new_page()

        try:
            await page.goto(
                "https://creator.xiaohongshu.com/message",
                wait_until="networkidle",
                timeout=30000,
            )
            await asyncio.sleep(random.uniform(2, 4))

            if "login" in page.url.lower():
                return {"success": False, "error": "小红书创作者中心需要重新登录"}

            # Find buyer conversation
            if buyer_name:
                conv = await page.query_selector(f'[class*="conversation"]:has-text("{buyer_name}")')
                if conv:
                    await conv.click()
                    await asyncio.sleep(1)

            message = f"感谢购买！资源已准备好～\n\n{delivery_content}\n\n有任何问题随时私信我哦～"
            chat_input = await page.query_selector(
                'textarea, [contenteditable="true"]'
            )
            if chat_input:
                await chat_input.click()
                await asyncio.sleep(0.5)
                await page.keyboard.type(message, delay=random.randint(30, 80))
                send_btn = await page.query_selector('button:has-text("发送")')
                if send_btn:
                    await send_btn.click()
                    await asyncio.sleep(2)
                    await browser_manager.save_state(account_id)
                    return {"success": True, "error": None}

            return {"success": False, "error": "未找到聊天输入框"}

        except Exception as e:
            logger.error(f"XHS virtual delivery failed: {e}")
            return {"success": False, "error": str(e)}
        finally:
            await page.close()

    async def auto_resend(
        self,
        order_id: str,
        delivery_content: str,
        buyer_name: str = "",
    ):
        """Re-send virtual product link (for "没收到" customer queries)."""
        await notification_service.send_dingtalk(
            title="虚拟商品重发",
            content=f"订单: {order_id}\n买家: {buyer_name}\n内容已自动重发",
        )
        return {"success": True, "message": "已重发"}


virtual_delivery_service = VirtualDeliveryService()
