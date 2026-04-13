from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.order import Order, Logistics
from app.models.system import User, Account
from app.services.orders.refund import refund_service
from app.services.notification import notification_service

router = APIRouter()


@router.get("/", summary="订单列表")
async def list_orders(
    status: str | None = None,
    sale_platform: str | None = None,
    order_type: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(Order)
    if status:
        query = query.where(Order.status == status)
    if sale_platform:
        query = query.where(Order.sale_platform == sale_platform)
    if order_type:
        query = query.where(Order.order_type == order_type)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = (
        query.options(selectinload(Order.logistics))
        .order_by(Order.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    orders = (await db.execute(query)).scalars().unique().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_order_to_dict(o) for o in orders],
    }


@router.get("/stats", summary="订单统计")
async def order_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    statuses = ["pending", "purchasing", "purchased", "shipped", "delivered", "completed", "refunding", "refunded", "error"]
    counts = {}
    for s in statuses:
        count_q = select(func.count()).where(Order.status == s)
        counts[s] = (await db.execute(count_q)).scalar() or 0

    total_profit_q = select(func.sum(Order.actual_profit)).where(
        Order.status.in_(["completed", "delivered"])
    )
    total_profit = (await db.execute(total_profit_q)).scalar() or 0

    total_revenue_q = select(func.sum(Order.sale_price)).where(
        Order.status.in_(["completed", "delivered", "shipped"])
    )
    total_revenue = (await db.execute(total_revenue_q)).scalar() or 0

    return {
        "status_counts": counts,
        "total_profit": round(total_profit, 2),
        "total_revenue": round(total_revenue, 2),
        "total_orders": sum(counts.values()),
    }


@router.get("/{order_id}", summary="订单详情")
async def get_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Order).where(Order.id == order_id).options(selectinload(Order.logistics))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return _order_to_dict(order)


class ManualPurchaseRequest(BaseModel):
    source_platform: str
    source_order_id: str
    purchase_cost: float


@router.post("/{order_id}/manual-purchase", summary="手动录入采购信息")
async def manual_purchase(
    order_id: str,
    req: ManualPurchaseRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    order.source_platform = req.source_platform
    order.source_order_id = req.source_order_id
    order.purchase_cost = req.purchase_cost
    order.source_order_status = "paid"
    order.status = "purchased"
    order.purchased_at = datetime.now(timezone.utc)
    order.actual_profit = round(order.sale_price - order.platform_fee - req.purchase_cost - order.shipping_cost, 2)
    order.error_message = None
    await db.commit()
    return {"message": "采购信息已录入", "status": "purchased"}


class ReturnRequest(BaseModel):
    reason: str = ""


@router.post("/{order_id}/return", summary="处理退货申请")
async def handle_return(
    order_id: str,
    req: ReturnRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    if not order.source_order_id or not order.source_platform:
        order.status = "refunding"
        await db.commit()
        await notification_service.notify(
            "退货需手动处理",
            f"订单 {order.sale_order_id} 无源平台信息，需手动处理退货",
            level="warning",
        )
        return {"message": "无源平台信息，已标记为退款中，请手动处理", "status": "refunding"}

    # Get source account
    source_acct = await db.execute(
        select(Account).where(
            Account.platform == order.source_platform,
            Account.is_active == True,
        ).limit(1)
    )
    source_account = source_acct.scalar_one_or_none()
    if not source_account:
        order.status = "refunding"
        await db.commit()
        return {"message": "无可用采购账号，需手动处理退货", "status": "refunding"}

    config = {"proxy_url": source_account.proxy_url, "user_agent": source_account.user_agent}

    result_data = await refund_service.handle_buyer_return_request(
        order_id=str(order.id),
        sale_order_id=order.sale_order_id,
        buyer_name=order.buyer_name or "",
        item_title="",
        source_platform=order.source_platform,
        source_order_id=order.source_order_id,
        source_account_id=str(source_account.id),
        source_account_config=config,
    )

    order.status = "refunding"
    await db.commit()

    return {
        "message": result_data["message"],
        "status": "refunding",
        "return_address": result_data.get("return_address"),
    }


class ReturnTrackingRequest(BaseModel):
    carrier: str
    tracking_number: str


@router.post("/{order_id}/return-tracking", summary="录入退货快递单号")
async def submit_return_tracking(
    order_id: str,
    req: ReturnTrackingRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    # Record return logistics
    return_logistics = Logistics(
        order_id=order.id,
        direction="return",
        carrier=req.carrier,
        tracking_number=req.tracking_number,
        status="in_transit",
    )
    db.add(return_logistics)

    # Submit to source platform
    if order.source_order_id and order.source_platform:
        source_acct = await db.execute(
            select(Account).where(
                Account.platform == order.source_platform,
                Account.is_active == True,
            ).limit(1)
        )
        source_account = source_acct.scalar_one_or_none()
        if source_account:
            config = {"proxy_url": source_account.proxy_url, "user_agent": source_account.user_agent}
            await refund_service.submit_return_tracking(
                source_platform=order.source_platform,
                source_order_id=order.source_order_id,
                source_account_id=str(source_account.id),
                source_account_config=config,
                return_carrier=req.carrier,
                return_tracking_number=req.tracking_number,
            )

    await db.commit()
    return {"message": "退货快递已录入并提交到源平台"}


@router.post("/{order_id}/confirm-refund", summary="确认退款完成")
async def confirm_refund(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    order.status = "refunded"
    order.actual_profit = 0
    await db.commit()
    return {"message": "退款已确认", "status": "refunded"}


def _order_to_dict(o: Order) -> dict:
    return {
        "id": str(o.id),
        "sale_platform": o.sale_platform,
        "sale_order_id": o.sale_order_id,
        "buyer_name": o.buyer_name,
        "buyer_phone": o.buyer_phone,
        "buyer_address": o.buyer_address,
        "buyer_note": o.buyer_note,
        "sale_price": o.sale_price,
        "platform_fee": o.platform_fee,
        "purchase_cost": o.purchase_cost,
        "shipping_cost": o.shipping_cost,
        "actual_profit": o.actual_profit,
        "source_platform": o.source_platform,
        "source_order_id": o.source_order_id,
        "status": o.status,
        "order_type": o.order_type,
        "error_message": o.error_message,
        "paid_at": o.paid_at.isoformat() if o.paid_at else None,
        "purchased_at": o.purchased_at.isoformat() if o.purchased_at else None,
        "shipped_at": o.shipped_at.isoformat() if o.shipped_at else None,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "logistics": [
            {
                "id": str(l.id),
                "direction": l.direction,
                "carrier": l.carrier,
                "tracking_number": l.tracking_number,
                "status": l.status,
                "synced_to_sale_platform": l.synced_to_sale_platform,
            }
            for l in (o.logistics or [])
        ],
    }
