"""
Refund and return handling service.
Manages the return flow between buyer (Xianyu) and source merchant.
"""
import re
from datetime import datetime, timezone

from loguru import logger

from app.services.orders.logistics import get_source_return_address
from app.services.notification import notification_service


class RefundService:

    async def handle_buyer_return_request(
        self,
        order_id: str,
        sale_order_id: str,
        buyer_name: str,
        item_title: str,
        source_platform: str,
        source_order_id: str,
        source_account_id: str,
        source_account_config: dict,
    ) -> dict:
        """
        Handle a buyer return request on Xianyu.

        Flow:
        1. Initiate return on source platform
        2. Get source merchant return address
        3. Push notification with return address
        4. User communicates return address to buyer
        """
        logger.info(f"Handling return request for order {sale_order_id}")

        # Step 1: Get source merchant return address
        addr_result = await get_source_return_address(
            account_id=source_account_id,
            account_config=source_account_config,
            source_platform=source_platform,
            source_order_id=source_order_id,
        )

        if not addr_result["success"]:
            # Notify for manual handling
            await notification_service.send_dingtalk(
                title="⚠️ 退货地址获取失败",
                content=(
                    f"订单: {sale_order_id}\n"
                    f"商品: {item_title}\n"
                    f"买家: {buyer_name}\n"
                    f"源平台: {source_platform}\n"
                    f"源订单: {source_order_id}\n"
                    f"错误: {addr_result['error']}\n"
                    f"请手动在{source_platform}上发起退货并获取退货地址"
                ),
            )
            return {
                "status": "needs_manual",
                "message": "退货地址获取失败，已推送通知",
                "return_address": None,
            }

        # Step 2: Push notification with return address
        address = addr_result["address"]
        contact = addr_result["contact_name"]
        phone = addr_result["contact_phone"]

        msg = (
            f"📦 买家申请退货\n\n"
            f"闲鱼订单: {sale_order_id}\n"
            f"买家: {buyer_name}\n"
            f"商品: {item_title}\n\n"
            f"源商家退货地址:\n"
            f"  地址: {address}\n"
            f"  联系人: {contact}\n"
            f"  电话: {phone}\n\n"
            f"请在闲鱼聊天中把以上退货地址发给买家，"
            f"让买家直接寄到源商家地址。\n"
            f"买家寄出后请录入退货快递单号。"
        )

        await notification_service.send_dingtalk(title="买家退货通知", content=msg)

        return {
            "status": "address_obtained",
            "message": "退货地址已获取，已推送通知",
            "return_address": {
                "address": address,
                "contact_name": contact,
                "contact_phone": phone,
            },
        }

    async def submit_return_tracking(
        self,
        source_platform: str,
        source_order_id: str,
        source_account_id: str,
        source_account_config: dict,
        return_carrier: str,
        return_tracking_number: str,
    ) -> dict:
        """
        Submit the buyer's return tracking number to the source platform.
        Called after the buyer ships the return.
        """
        from app.services.browser import browser_manager
        import asyncio, random

        context = await browser_manager.get_context(source_account_id, source_account_config)
        page = await context.new_page()

        try:
            if source_platform == "pinduoduo":
                url = f"https://mobile.yangkeduo.com/order.html?order_sn={source_order_id}"
            else:
                url = f"https://buyertrade.taobao.com/trade/detail/trade_order_detail.htm?biz_order_id={source_order_id}"

            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(random.uniform(2, 4))

            # Find return tracking input
            tracking_input = await page.query_selector(
                'input[placeholder*="快递单号"], input[placeholder*="退货单号"]'
            )
            if tracking_input:
                await tracking_input.click()
                await asyncio.sleep(0.5)
                await page.keyboard.type(return_tracking_number, delay=random.randint(80, 150))

            carrier_input = await page.query_selector(
                'input[placeholder*="快递公司"], select[class*="carrier"]'
            )
            if carrier_input:
                await carrier_input.click()
                await asyncio.sleep(0.5)
                await page.keyboard.type(return_carrier, delay=random.randint(80, 150))

            submit_btn = await page.query_selector('button:has-text("提交"), button:has-text("确认")')
            if submit_btn:
                await submit_btn.click()
                await asyncio.sleep(3)

            await browser_manager.save_state(source_account_id)
            logger.info(f"Return tracking submitted: {return_carrier} {return_tracking_number}")
            return {"success": True, "error": None}

        except Exception as e:
            logger.error(f"Return tracking submission failed: {e}")
            return {"success": False, "error": str(e)}
        finally:
            await page.close()

    async def handle_refund_completion(
        self,
        order_id: str,
        sale_order_id: str,
        item_title: str,
        refund_amount: float,
    ) -> dict:
        """
        Handle refund completion after source merchant confirms return.
        Notify user to process Xianyu refund.
        """
        msg = (
            f"✅ 源平台退款已到账\n\n"
            f"订单: {sale_order_id}\n"
            f"商品: {item_title}\n"
            f"退款金额: ¥{refund_amount}\n\n"
            f"请在闲鱼确认退款给买家。"
        )

        await notification_service.send_dingtalk(title="退款到账通知", content=msg)

        return {"status": "refund_received", "message": "源平台退款已到账，请在闲鱼确认退款"}


refund_service = RefundService()
