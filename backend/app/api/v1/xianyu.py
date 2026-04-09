from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.xianyu import XianyuListing, XianyuMarketData
from app.models.system import User

router = APIRouter()


@router.get("/listings", summary="闲鱼发布列表")
async def list_listings(
    status: str | None = None,
    account_id: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(XianyuListing)
    if status:
        query = query.where(XianyuListing.status == status)
    if account_id:
        query = query.where(XianyuListing.account_id == account_id)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(XianyuListing.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    listings = (await db.execute(query)).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": str(l.id),
                "title": l.title,
                "price": l.price,
                "original_cost": l.original_cost,
                "expected_profit": l.expected_profit,
                "status": l.status,
                "views": l.views,
                "wants": l.wants,
                "chats": l.chats,
                "published_at": l.published_at.isoformat() if l.published_at else None,
                "last_refreshed_at": l.last_refreshed_at.isoformat() if l.last_refreshed_at else None,
            }
            for l in listings
        ],
    }


@router.post("/listings", summary="创建闲鱼草稿")
async def create_listing(
    product_id: str,
    account_id: str,
    title: str,
    description: str,
    price: float,
    original_cost: float,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    listing = XianyuListing(
        product_id=product_id,
        account_id=account_id,
        title=title,
        description=description,
        price=price,
        original_cost=original_cost,
        expected_profit=round(price - original_cost - price * 0.006, 2),
        status="draft",
    )
    db.add(listing)
    await db.commit()
    await db.refresh(listing)
    return {"id": str(listing.id), "status": "draft", "message": "草稿已创建"}


@router.put("/listings/{listing_id}", summary="更新闲鱼草稿")
async def update_listing(
    listing_id: str,
    title: str | None = None,
    description: str | None = None,
    price: float | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(XianyuListing).where(XianyuListing.id == listing_id))
    listing = result.scalar_one_or_none()
    if not listing:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    if title:
        listing.title = title
    if description:
        listing.description = description
    if price:
        listing.price = price
        listing.expected_profit = round(price - listing.original_cost - price * 0.006, 2)
    await db.commit()
    return {"message": "已更新"}


@router.post("/listings/{listing_id}/publish", summary="发布到闲鱼")
async def publish_listing(
    listing_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(XianyuListing).where(XianyuListing.id == listing_id))
    listing = result.scalar_one_or_none()
    if not listing:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    if listing.status not in ("draft", "error"):
        raise HTTPException(status_code=400, detail=f"当前状态 {listing.status} 不允许发布")

    listing.status = "pending_review"
    await db.commit()

    # TODO: Trigger Celery task for Playwright-based publishing
    return {"message": "已加入发布队列", "status": "pending_review"}


@router.post("/listings/batch-refresh", summary="批量擦亮")
async def batch_refresh(
    listing_ids: list[str],
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(XianyuListing).where(
            XianyuListing.id.in_(listing_ids),
            XianyuListing.status == "published",
        )
    )
    listings = result.scalars().all()
    # TODO: Trigger Celery task for batch refresh via Playwright
    return {"message": f"已加入擦亮队列: {len(listings)}个商品"}


@router.get("/market/{product_id}", summary="闲鱼市场数据")
async def get_market_data(
    product_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(XianyuMarketData)
        .where(XianyuMarketData.product_id == product_id)
        .order_by(XianyuMarketData.captured_at.desc())
        .limit(10)
    )
    records = result.scalars().all()
    if not records:
        return {"message": "暂无市场数据", "records": []}

    return {
        "latest": {
            "active_listings": records[0].active_listings,
            "total_wants": records[0].total_wants,
            "price_min": records[0].price_min,
            "price_max": records[0].price_max,
            "price_avg": records[0].price_avg,
            "price_cv": records[0].price_cv,
            "top5_sales": records[0].top5_sales,
            "seller_distribution": records[0].seller_distribution,
            "captured_at": records[0].captured_at.isoformat(),
        },
        "history": [
            {
                "active_listings": r.active_listings,
                "price_avg": r.price_avg,
                "price_cv": r.price_cv,
                "total_wants": r.total_wants,
                "captured_at": r.captured_at.isoformat(),
            }
            for r in records
        ],
    }
