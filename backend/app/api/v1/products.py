from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.product import Product
from app.models.system import User
from app.schemas.product import ProductOut

router = APIRouter()


class SearchRequest(BaseModel):
    keyword: str
    platform: str = "xianyu"


@router.post("/search", summary="提交搜索任务")
async def search_products(
    req: SearchRequest,
    user: User = Depends(get_current_user),
):
    from app.tasks.selection import instant_search
    instant_search.delay(req.keyword, req.platform)
    return {"message": "搜索任务已提交", "keyword": req.keyword, "platform": req.platform}


@router.get("/", summary="商品列表")
async def list_products(
    platform: str | None = None,
    category: str | None = None,
    product_type: str | None = None,
    is_active: bool | None = True,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(Product)
    if platform:
        query = query.where(Product.source_platform == platform)
    if category:
        query = query.where(Product.category == category)
    if product_type:
        query = query.where(Product.product_type == product_type)
    if is_active is not None:
        query = query.where(Product.is_active == is_active)
    if search:
        query = query.where(Product.title.ilike(f"%{search}%"))

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    query = query.order_by(Product.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    products = result.scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [ProductOut.model_validate(p).model_dump() for p in products],
    }


@router.get("/{product_id}", summary="商品详情")
async def get_product(
    product_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")
    return ProductOut.model_validate(product).model_dump()


@router.delete("/{product_id}", summary="删除商品")
async def delete_product(
    product_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")
    product.is_active = False
    await db.commit()
    return {"message": "已删除"}
