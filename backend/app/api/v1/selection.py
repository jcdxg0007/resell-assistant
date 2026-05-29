from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.product import Product, ProductScore
from app.models.xianyu import XianyuMarketData
from app.models.system import User
from app.schemas.product import ScoreRequest
from app.services.selection.scoring import (
    ProductScoringInput, calculate_product_score, DECISION_LABELS,
    PriceStats,
)
from app.services.selection.pricing import smart_pricing

router = APIRouter()


@router.get("/xianyu/recommendations", summary="闲鱼选品推荐列表")
async def xianyu_recommendations(
    min_score: float = Query(0, ge=0, le=100),
    category: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = (
        select(Product, ProductScore)
        .join(ProductScore, ProductScore.product_id == Product.id)
        .where(ProductScore.score_type == "product_10d")
        .where(ProductScore.total_score >= min_score)
        .where(Product.is_active == True)
    )
    if category:
        query = query.where(Product.category == category)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(ProductScore.total_score.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(query)).all()

    items = []
    for product, score in rows:
        items.append({
            "product": {
                "id": str(product.id),
                "title": product.title,
                "source_platform": product.source_platform,
                "price": product.price,
                "category": product.category,
                "image_urls": product.image_urls,
            },
            "score": {
                "total_score": score.total_score,
                "decision": score.decision,
                "decision_label": DECISION_LABELS.get(score.decision, score.decision),
                "dimensions": score.dimension_scores,
                "scored_at": score.scored_at.isoformat() if score.scored_at else None,
            },
        })

    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.delete("/xianyu/products", summary="清空闲鱼采集结果（给 category 则只清该词，否则清全部）")
async def clear_xianyu_products(
    category: str | None = Query(None, description="只清这个关键词；留空清全部闲鱼采集结果"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """硬删除闲鱼采集到的商品行（物理 DELETE，不可恢复）。

    与 PDD 的 DELETE /pdd-runs/today 对齐：清当前词 = category 给定；清全部 = 留空。
    products.id 的外键子表（ProductScore / KeywordProduct / XianyuMarketData /
    ProductImage 等）均为 ON DELETE CASCADE，删父行时数据库自动级联清理；
    orders / conversations 等业务引用为 SET NULL，不受影响。
    """
    stmt = delete(Product).where(Product.source_platform == "xianyu")
    if category:
        stmt = stmt.where(Product.category == category)
    res = await db.execute(stmt)
    await db.commit()
    return {"ok": True, "deleted": res.rowcount or 0}


@router.post("/score/{product_id}", summary="触发商品评分")
async def score_product(
    product_id: str,
    req: ScoreRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")

    # Get latest market data
    market_result = await db.execute(
        select(XianyuMarketData)
        .where(XianyuMarketData.product_id == product_id)
        .order_by(XianyuMarketData.captured_at.desc())
        .limit(1)
    )
    market = market_result.scalar_one_or_none()

    active_listings = market.active_listings if market else 0
    xianyu_avg = market.price_avg if market else None
    price_min = (market.price_min if market else None) or 0.0
    price_max = (market.price_max if market else None) or 0.0

    # Manual-score entry point doesn't run the full clean_keyword_sample path,
    # so we fabricate a PriceStats from the stored market aggregates. Using
    # avg as median and min/max as the quartile edges is an approximation,
    # but good enough for a one-off score refresh on an individual product.
    price_stats = PriceStats(
        median=xianyu_avg or 0.0,
        p25=price_min,
        p75=price_max,
        sample_size=active_listings or 0,
        suspicious_count=0,
    )
    scoring_input = ProductScoringInput(
        price=float(product.price or 0),
        item_wants=int(product.sales_count or 0),
        title=product.title or "",
        relevance_score=10.0,  # manual score path trusts the operator
        price_stats=price_stats,
        taobao_match_price=req.source_price if req.source_price else None,
        estimated_cost=(req.source_price + req.shipping_fee) if req.source_price else None,
    )

    score_result = calculate_product_score(scoring_input)

    # Pricing suggestion
    top5_prices = [i["price"] for i in (market.top5_sales or [])] if market and market.top5_sales else []
    pricing = smart_pricing(
        cost=req.source_price,
        shipping=req.shipping_fee,
        xianyu_active_listings=active_listings,
        xianyu_avg_price=xianyu_avg,
        xianyu_top5_prices=top5_prices or None,
    )

    # Save score to DB
    dim_dict = {d.name: {"score": d.score, "max": d.max_score, "label": d.label} for d in score_result.dimensions}
    db_score = ProductScore(
        product_id=product_id,
        score_type="product_10d",
        total_score=score_result.total_score,
        dimension_scores=dim_dict,
        decision=score_result.decision,
        scored_at=datetime.now(timezone.utc),
    )
    db.add(db_score)
    await db.commit()

    return {
        "score": {
            "total_score": score_result.total_score,
            "decision": score_result.decision,
            "decision_label": DECISION_LABELS.get(score_result.decision),
            "dimensions": dim_dict,
        },
        "pricing": {
            "mode": pricing.mode,
            "recommended_price": pricing.recommended_price,
            "price_floor": pricing.price_floor,
            "estimated_profit": pricing.estimated_profit,
            "profit_margin": pricing.profit_margin,
            "breakdown": pricing.breakdown,
        },
    }


@router.get("/xhs/recommendations", summary="小红书选品推荐")
async def xhs_recommendations(
    min_score: float = Query(0, ge=0, le=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = (
        select(Product, ProductScore)
        .join(ProductScore, ProductScore.product_id == Product.id)
        .where(ProductScore.score_type == "xhs_5d")
        .where(ProductScore.total_score >= min_score)
        .where(Product.is_active == True)
    )
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(ProductScore.total_score.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(query)).all()

    items = []
    for product, score in rows:
        items.append({
            "product": {"id": str(product.id), "title": product.title, "price": product.price, "image_urls": product.image_urls},
            "score": {"total_score": score.total_score, "decision": score.decision, "dimensions": score.dimension_scores},
        })
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.get("/xhs/trending", summary="小红书热门趋势")
async def xhs_trending(
    user: User = Depends(get_current_user),
):
    # TODO: Query xhs_hot_topics and xhs_trending_keywords
    return {"topics": [], "keywords": [], "message": "数据采集启动后将自动填充"}


@router.get("/virtual/recommendations", summary="虚拟商品推荐")
async def virtual_recommendations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(Product).where(Product.product_type == "virtual").where(Product.is_active == True)
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(Product.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    products = (await db.execute(query)).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [{"id": str(p.id), "title": p.title, "price": p.price, "category": p.category} for p in products],
    }
