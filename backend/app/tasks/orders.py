"""
Order fulfillment Celery tasks.
Handles order detection, auto-purchase, logistics sync, and refund monitoring.
"""
import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.database import AsyncSessionLocal
from app.models.order import Order, Logistics
from app.models.system import Account
from app.services.orders.detector import detect_xianyu_orders
from app.services.orders.fulfillment import fulfillment_service
from app.services.orders.logistics import check_source_shipment, sync_tracking_to_xianyu
from app.services.orders.refund import refund_service
from app.services.notification import notification_service


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.orders.detect_new_orders")
def detect_new_orders():
    """Poll all active Xianyu accounts for new orders (every 3 min)."""
    logger.info("Starting order detection cycle")
    run_async(_detect_new_orders())


async def _get_purchase_mode() -> str:
    """Read auto_purchase_mode from SystemConfig (defaults to 'manual')."""
    try:
        from app.models.system import SystemConfig
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SystemConfig.value).where(SystemConfig.key == "auto_purchase_mode")
            )
            row = result.scalar_one_or_none()
            return row if row else "manual"
    except Exception:
        return "manual"


async def _detect_new_orders():
    purchase_mode = await _get_purchase_mode()
    logger.info(f"Purchase mode: {purchase_mode}")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Account).where(
                Account.platform == "xianyu",
                Account.is_active == True,
                Account.lifecycle_stage.in_(["growing", "mature"]),
            )
        )
        accounts = result.scalars().all()

        for account in accounts:
            try:
                known = await db.execute(
                    select(Order.sale_order_id).where(Order.account_id == account.id)
                )
                known_ids = {row[0] for row in known.all()}

                config = {
                    "proxy_url": account.proxy_url,
                    "user_agent": account.user_agent,
                    "viewport": account.viewport,
                }

                new_orders = await detect_xianyu_orders(
                    account_id=str(account.id),
                    account_config=config,
                    known_order_ids=known_ids,
                )

                for order_data in new_orders:
                    order = Order(
                        sale_platform="xianyu",
                        sale_order_id=order_data["sale_order_id"],
                        account_id=account.id,
                        buyer_name=order_data.get("buyer_name"),
                        buyer_address=order_data.get("buyer_address"),
                        buyer_phone=order_data.get("buyer_phone"),
                        buyer_note=order_data.get("buyer_note"),
                        sale_price=order_data.get("sale_price", 0),
                        platform_fee=round(order_data.get("sale_price", 0) * 0.006, 2),
                        sku_info=order_data.get("sku_info"),
                        status="pending",
                        paid_at=datetime.now(timezone.utc),
                    )
                    db.add(order)

                if new_orders:
                    await db.commit()
                    logger.info(f"Account {account.account_name}: {len(new_orders)} new orders")

                    for order_data in new_orders:
                        if purchase_mode == "auto":
                            await notification_service.notify_new_order(order_data)
                            auto_purchase_order.delay(order_data["sale_order_id"])
                        else:
                            await notification_service.notify_new_order_manual(order_data)
                            logger.info(f"Manual mode: DingTalk notification sent for {order_data['sale_order_id']}")

            except Exception as e:
                logger.error(f"Order detection failed for {account.account_name}: {e}")


@celery_app.task(name="app.tasks.orders.auto_purchase_order", bind=True, max_retries=2)
def auto_purchase_order(self, sale_order_id: str):
    """Auto-purchase on source platform for a specific order."""
    logger.info(f"Auto-purchase triggered for order {sale_order_id}")
    run_async(_auto_purchase(sale_order_id))


async def _auto_purchase(sale_order_id: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Order).where(Order.sale_order_id == sale_order_id)
        )
        order = result.scalar_one_or_none()
        if not order or order.status != "pending":
            return

        # Find linked product and source info
        if not order.product_id:
            order.status = "error"
            order.error_message = "未关联源商品"
            await db.commit()
            await notification_service.notify_order_error(sale_order_id, "未关联源商品，需手动处理")
            return

        from app.models.product import Product
        prod_result = await db.execute(select(Product).where(Product.id == order.product_id))
        product = prod_result.scalar_one_or_none()
        if not product:
            order.status = "error"
            order.error_message = "源商品不存在"
            await db.commit()
            return

        # Parse buyer address into structured format
        buyer_address = _parse_address(order.buyer_address or "", order.buyer_name, order.buyer_phone)

        # Get source platform account for purchasing
        source_acct = await db.execute(
            select(Account).where(
                Account.platform == product.source_platform,
                Account.is_active == True,
            ).limit(1)
        )
        source_account = source_acct.scalar_one_or_none()
        if not source_account:
            order.status = "error"
            order.error_message = f"无可用的{product.source_platform}采购账号"
            await db.commit()
            await notification_service.notify_order_error(sale_order_id, order.error_message)
            return

        config = {
            "proxy_url": source_account.proxy_url,
            "user_agent": source_account.user_agent,
            "viewport": source_account.viewport,
        }

        order.status = "purchasing"
        await db.commit()

        # Execute purchase
        if product.source_platform in ("pinduoduo", "pdd"):
            purchase_result = await fulfillment_service.auto_purchase_pdd(
                account_id=str(source_account.id),
                account_config=config,
                source_url=product.source_url,
                buyer_address=buyer_address,
                sku_mapping=order.source_sku_mapping,
                expected_price=product.price,
            )
        elif product.source_platform in ("taobao", "tb"):
            purchase_result = await fulfillment_service.auto_purchase_taobao(
                account_id=str(source_account.id),
                account_config=config,
                source_url=product.source_url,
                buyer_address=buyer_address,
                sku_mapping=order.source_sku_mapping,
                expected_price=product.price,
            )
        else:
            order.status = "error"
            order.error_message = f"不支持的源平台: {product.source_platform}"
            await db.commit()
            return

        if purchase_result["success"]:
            order.status = "purchased"
            order.source_platform = product.source_platform
            order.source_order_id = purchase_result["source_order_id"]
            order.source_order_status = "paid"
            order.purchase_cost = purchase_result["actual_price"]
            order.purchased_at = datetime.now(timezone.utc)
            order.actual_profit = round(
                order.sale_price - order.platform_fee - purchase_result["actual_price"] - order.shipping_cost, 2
            )
            await db.commit()
            logger.info(f"Order {sale_order_id} purchased: source={purchase_result['source_order_id']}")
        else:
            if purchase_result.get("needs_manual"):
                order.status = "error"
                order.error_message = purchase_result["error"]
                await db.commit()
                await notification_service.notify_order_error(sale_order_id, purchase_result["error"])
            else:
                order.status = "error"
                order.error_message = purchase_result["error"]
                await db.commit()


@celery_app.task(name="app.tasks.orders.sync_logistics")
def sync_logistics():
    """Check and sync logistics for purchased orders (every 30 min)."""
    logger.info("Starting logistics sync cycle")
    run_async(_sync_logistics())


async def _sync_logistics():
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Order).where(
                Order.status.in_(["purchased", "shipped"]),
                Order.source_order_id.isnot(None),
            ).limit(50)
        )
        orders = result.scalars().all()

        for order in orders:
            try:
                # Get source account
                source_acct = await db.execute(
                    select(Account).where(
                        Account.platform == order.source_platform,
                        Account.is_active == True,
                    ).limit(1)
                )
                source_account = source_acct.scalar_one_or_none()
                if not source_account:
                    continue

                config = {
                    "proxy_url": source_account.proxy_url,
                    "user_agent": source_account.user_agent,
                }

                ship_result = await check_source_shipment(
                    account_id=str(source_account.id),
                    account_config=config,
                    source_platform=order.source_platform,
                    source_order_id=order.source_order_id,
                )

                if ship_result["shipped"] and ship_result["tracking_number"]:
                    # Check if we already have this tracking
                    existing = await db.execute(
                        select(Logistics).where(
                            Logistics.order_id == order.id,
                            Logistics.direction == "forward",
                        )
                    )
                    logistics = existing.scalar_one_or_none()

                    if not logistics:
                        logistics = Logistics(
                            order_id=order.id,
                            direction="forward",
                            carrier=ship_result["carrier"],
                            tracking_number=ship_result["tracking_number"],
                            status="in_transit",
                            tracking_events=ship_result["events"],
                            last_tracked_at=datetime.now(timezone.utc),
                        )
                        db.add(logistics)
                    else:
                        logistics.tracking_events = ship_result["events"]
                        logistics.last_tracked_at = datetime.now(timezone.utc)

                    # Sync to Xianyu if not already done
                    if not logistics.synced_to_sale_platform:
                        sale_acct = await db.execute(
                            select(Account).where(Account.id == order.account_id)
                        )
                        sale_account = sale_acct.scalar_one_or_none()
                        if sale_account:
                            sale_config = {
                                "proxy_url": sale_account.proxy_url,
                                "user_agent": sale_account.user_agent,
                            }
                            sync_result = await sync_tracking_to_xianyu(
                                account_id=str(sale_account.id),
                                account_config=sale_config,
                                sale_order_id=order.sale_order_id,
                                carrier=ship_result["carrier"] or "其他",
                                tracking_number=ship_result["tracking_number"],
                            )
                            if sync_result["success"]:
                                logistics.synced_to_sale_platform = True
                                order.status = "shipped"
                                order.shipped_at = datetime.now(timezone.utc)
                                logger.info(f"Tracking synced for {order.sale_order_id}")

                    await db.commit()

            except Exception as e:
                logger.error(f"Logistics sync failed for {order.sale_order_id}: {e}")


@celery_app.task(name="app.tasks.orders.check_refund_status")
def check_refund_status():
    """Monitor refunding orders for status changes."""
    logger.info("Checking refund statuses")
    run_async(_check_refunds())


async def _check_refunds():
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Order).where(Order.status == "refunding")
        )
        orders = result.scalars().all()

        for order in orders:
            # TODO: Check source platform refund status via Playwright
            logger.debug(f"Checking refund for order {order.sale_order_id}")


def _parse_address(raw: str, name: str = "", phone: str = "") -> dict:
    """Parse a raw address string into structured format."""
    import re
    provinces = [
        "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
        "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
        "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾",
        "内蒙古", "广西", "西藏", "宁夏", "新疆",
    ]

    result = {"name": name, "phone": phone, "province": "", "city": "", "district": "", "detail": raw}

    for p in provinces:
        if p in raw:
            result["province"] = p if not p.endswith("省") else p
            idx = raw.index(p) + len(p)
            if idx < len(raw) and raw[idx] in "省市":
                idx += 1
            remaining = raw[idx:].strip()

            city_match = re.match(r'(.+?)[市州盟]', remaining)
            if city_match:
                result["city"] = city_match.group(0)
                remaining = remaining[len(result["city"]):].strip()

            district_match = re.match(r'(.+?)[区县旗]', remaining)
            if district_match:
                result["district"] = district_match.group(0)
                remaining = remaining[len(result["district"]):].strip()

            result["detail"] = remaining
            break

    return result
