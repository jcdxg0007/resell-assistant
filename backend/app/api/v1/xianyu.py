from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.xianyu import XianyuListing, XianyuMarketData
from app.models.system import User

router = APIRouter()


def _listing_to_dict(l: XianyuListing) -> dict:
    return {
        "id": str(l.id),
        "product_id": str(l.product_id),
        "account_id": str(l.account_id),
        "xianyu_item_id": l.xianyu_item_id,
        "title": l.title,
        "description": l.description,
        "price": l.price,
        "original_cost": l.original_cost,
        "expected_profit": l.expected_profit,
        "image_paths": l.image_paths,
        "status": l.status,
        "error_message": l.error_message,
        "views": l.views,
        "wants": l.wants,
        "chats": l.chats,
        "published_at": l.published_at.isoformat() if l.published_at else None,
        "last_refreshed_at": l.last_refreshed_at.isoformat() if l.last_refreshed_at else None,
        "created_at": l.created_at.isoformat() if hasattr(l, "created_at") and l.created_at else None,
    }


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
        "items": [_listing_to_dict(l) for l in listings],
    }


class CreateListingRequest(BaseModel):
    product_id: str
    account_id: str
    title: str
    description: str
    price: float
    original_cost: float
    image_paths: list[str] | None = None


@router.post("/listings", summary="创建闲鱼草稿")
async def create_listing(
    req: CreateListingRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    listing = XianyuListing(
        product_id=req.product_id,
        account_id=req.account_id,
        title=req.title,
        description=req.description,
        price=req.price,
        original_cost=req.original_cost,
        expected_profit=round(req.price - req.original_cost - req.price * 0.006, 2),
        image_paths=req.image_paths,
        status="draft",
    )
    db.add(listing)
    await db.commit()
    await db.refresh(listing)
    return _listing_to_dict(listing)


class UpdateListingRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    price: float | None = None
    image_paths: list[str] | None = None


@router.put("/listings/{listing_id}", summary="更新闲鱼草稿")
async def update_listing(
    listing_id: str,
    req: UpdateListingRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(XianyuListing).where(XianyuListing.id == listing_id))
    listing = result.scalar_one_or_none()
    if not listing:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    if req.title is not None:
        listing.title = req.title
    if req.description is not None:
        listing.description = req.description
    if req.price is not None:
        listing.price = req.price
        listing.expected_profit = round(req.price - listing.original_cost - req.price * 0.006, 2)
    if req.image_paths is not None:
        listing.image_paths = req.image_paths
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
    listing.error_message = None
    await db.commit()

    from app.tasks.publish import execute_publish
    execute_publish.delay(str(listing.id))

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
    if not listings:
        raise HTTPException(status_code=400, detail="没有可擦亮的商品")

    from app.tasks.publish import execute_single_refresh
    for listing in listings:
        execute_single_refresh.delay(str(listing.id))

    return {"message": f"已加入擦亮队列: {len(listings)}个商品"}


@router.delete("/listings/{listing_id}", summary="删除/下架闲鱼商品")
async def remove_listing(
    listing_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(XianyuListing).where(XianyuListing.id == listing_id))
    listing = result.scalar_one_or_none()
    if not listing:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    listing.status = "removed"
    await db.commit()
    return {"message": "已下架"}


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
