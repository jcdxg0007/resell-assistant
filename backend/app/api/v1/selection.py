from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.product import Product, ProductScore, Platform
from app.models.xianyu import XianyuMarketData
from app.models.selection import SelectionAnalysis
from app.models.pdd_run import PddSearchRun
from app.models.pdd_pin import PddPin
from app.models.system import User
from app.schemas.product import ScoreRequest
from app.services.selection.scoring import (
    ProductScoringInput, calculate_product_score, DECISION_LABELS,
    PriceStats,
)
from app.services.selection.pricing import smart_pricing
from app.services.selection import ten_dim_scoring
from app.services.selection.ten_dim_scoring import pdd_fingerprint
from app.services.pdd_search_run import keyword_items, _cn_day_start

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


# ── Pin 收藏（保留商品、永不进每日清库）──────────────────────────────
def _pinned_to_dict(p: Product) -> dict:
    imgs = p.image_urls if isinstance(p.image_urls, list) else []
    return {
        "product_id": str(p.id),
        "title": p.title,
        "price": p.price,
        "source_platform": p.source_platform,
        "category": p.category,
        "item_wants": p.sales_count or 0,
        "seller_name": p.seller_name,
        "source_url": p.source_url,
        "image_url": imgs[0] if imgs else None,
        "pinned_at": p.pinned_at.isoformat() if p.pinned_at else None,
    }


@router.post("/products/{product_id}/pin", summary="Pin 一个商品（收藏，不进每日清库）")
async def pin_product(
    product_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = (await db.execute(select(Product).where(Product.id == product_id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    p.pinned_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "product_id": product_id, "pinned_at": p.pinned_at.isoformat()}


@router.delete("/products/{product_id}/pin", summary="取消 Pin")
async def unpin_product(
    product_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = (await db.execute(select(Product).where(Product.id == product_id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    p.pinned_at = None
    await db.commit()
    return {"ok": True, "product_id": product_id}


def _pdd_pin_to_dict(p: PddPin) -> dict:
    """PDD 快照收藏统一成和闲鱼 Pin 一样的结构（前端同一张表渲染）。

    PDD 无跳转链接/卖家：source_url 给空串、seller_name 给 None；item_wants 借位
    放 sales（前端「想要」列对 PDD 即销量）。product_id 用 pdd:<fingerprint>，
    与 score_pdd_side 生成的一致，收藏开关能对上。
    """
    return {
        "product_id": f"pdd:{p.fingerprint}",
        "title": p.title,
        "price": p.price,
        "source_platform": "pdd",
        "category": p.keyword,
        "item_wants": p.sales or 0,
        "badges": p.badges or [],
        "seller_name": None,
        "source_url": "",
        "image_url": p.image_url,
        "pinned_at": p.pinned_at.isoformat() if p.pinned_at else None,
    }


class PddPinBody(BaseModel):
    keyword: str | None = None
    title: str
    price: float | None = None
    sales: int | None = None
    badges: list[str] | None = None
    image_url: str | None = None


@router.post("/pdd-pin", summary="收藏一条 PDD 采集快照（冻结保存，跨日保留）")
async def pin_pdd_snapshot(
    body: PddPinBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fp = pdd_fingerprint(body.keyword, body.title)
    now = datetime.now(timezone.utc)
    existing = (await db.execute(
        select(PddPin).where(PddPin.fingerprint == fp)
    )).scalar_one_or_none()
    if existing:
        existing.keyword = body.keyword
        existing.title = body.title
        existing.price = body.price
        existing.sales = body.sales
        existing.badges = body.badges
        existing.image_url = body.image_url
        existing.pinned_at = now
    else:
        db.add(PddPin(
            fingerprint=fp,
            keyword=body.keyword,
            title=body.title,
            price=body.price,
            sales=body.sales,
            badges=body.badges,
            image_url=body.image_url,
            pinned_at=now,
        ))
    await db.commit()
    return {"ok": True, "product_id": f"pdd:{fp}", "pinned_at": now.isoformat()}


@router.delete("/pdd-pin/{fingerprint}", summary="取消 PDD 快照收藏")
async def unpin_pdd_snapshot(
    fingerprint: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    fp = fingerprint[4:] if fingerprint.startswith("pdd:") else fingerprint
    await db.execute(delete(PddPin).where(PddPin.fingerprint == fp))
    await db.commit()
    return {"ok": True, "product_id": f"pdd:{fp}"}


@router.get("/pinned", summary="已 Pin 收藏的商品列表（闲鱼真实商品 + PDD 快照）")
async def list_pinned(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(
        select(Product)
        .where(Product.pinned_at.isnot(None))
        .order_by(Product.pinned_at.desc())
    )).scalars().all()
    pdd_rows = (await db.execute(
        select(PddPin).order_by(PddPin.pinned_at.desc())
    )).scalars().all()
    items = [_pinned_to_dict(p) for p in rows] + [_pdd_pin_to_dict(p) for p in pdd_rows]
    # 两端合并后按收藏时间倒序统一排
    items.sort(key=lambda x: x.get("pinned_at") or "", reverse=True)
    return {"total": len(items), "items": items}


class PinnedDeleteBody(BaseModel):
    product_ids: list[str]


@router.post("/pinned/delete", summary="批量删除已 Pin 商品（物理删除，不可恢复）")
async def delete_pinned(
    body: PinnedDeleteBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not body.product_ids:
        return {"ok": True, "deleted": 0}
    # pdd:<fp> 走 pdd_pins，其余是闲鱼 Product.id
    pdd_fps = [pid[4:] for pid in body.product_ids if pid.startswith("pdd:")]
    xy_ids = [pid for pid in body.product_ids if not pid.startswith("pdd:")]
    deleted = 0
    if xy_ids:
        res = await db.execute(delete(Product).where(Product.id.in_(xy_ids)))
        deleted += res.rowcount or 0
    if pdd_fps:
        res = await db.execute(delete(PddPin).where(PddPin.fingerprint.in_(pdd_fps)))
        deleted += res.rowcount or 0
    await db.commit()
    return {"ok": True, "deleted": deleted}


@router.get("/xianyu/raw", summary="闲鱼采集原始挂牌（不依赖打分，多平台比价页用）")
async def xianyu_raw(
    category: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """只查 products 原始行，不 join product_scores。

    多平台比价页要的是「爬到什么就展示什么」的原始数据；打分挪到了
    「十维度选品」页（/selection/ten-dim）。
    """
    query = (
        select(Product)
        .where(Product.source_platform == Platform.XIANYU)
        .where(Product.is_active == True)
        # 只看今日采集（东八区日界），和 PDD 采集池口径对齐
        .where(Product.last_crawled_at >= _cn_day_start())
    )
    if category:
        query = query.where(Product.category == category)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(Product.last_crawled_at.desc().nullslast()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(query)).scalars().all()

    items = [{
        "product": {
            "id": str(p.id),
            "title": p.title,
            "source_platform": p.source_platform,
            "price": p.price,
            "category": p.category,
            "image_urls": p.image_urls,
            "item_wants": p.sales_count,
            "source_url": p.source_url,
            "seller_name": p.seller_name,
            "published_at": p.published_at.isoformat() if p.published_at else None,
        },
    } for p in rows]
    return {"total": total, "page": page, "page_size": page_size, "items": items}


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


async def _gather_xianyu_raw(db: AsyncSession, keyword: str) -> tuple[list[dict], int | None]:
    """取某关键词的闲鱼原始挂牌 + 同词最新在卖挂牌数。"""
    rows = (await db.execute(
        select(Product)
        .where(Product.source_platform == Platform.XIANYU)
        .where(Product.is_active == True)
        .where(Product.category == keyword)
    )).scalars().all()
    items = [{
        "product_id": str(p.id),
        "title": p.title,
        "price": p.price,
        "item_wants": p.sales_count or 0,
        "image_urls": p.image_urls,
        "published_at": p.published_at.isoformat() if p.published_at else None,
        "source_url": p.source_url,
        "seller_name": p.seller_name,
    } for p in rows]
    active_listings = (await db.execute(
        select(XianyuMarketData.active_listings)
        .where(XianyuMarketData.keyword == keyword)
        .order_by(XianyuMarketData.captured_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    return items, active_listings


async def _compute_and_cache(db: AsyncSession, keyword: str) -> dict:
    """实时算 A/B/C 并落 selection_analysis 缓存（upsert）。"""
    xy_items, active_listings = await _gather_xianyu_raw(db, keyword)
    pdd_res = await keyword_items(db, keyword)
    pdd_items = pdd_res.get("items") or []

    payload = ten_dim_scoring.analyze(
        keyword,
        xianyu_items=xy_items,
        pdd_items=pdd_items,
        active_listings=active_listings,
    )
    now = datetime.now(timezone.utc)
    existing = (await db.execute(
        select(SelectionAnalysis).where(SelectionAnalysis.keyword == keyword)
    )).scalar_one_or_none()
    if existing:
        existing.scored_at = now
        existing.xianyu_payload = payload["xianyu"]
        existing.pdd_payload = payload["pdd"]
        existing.arbitrage = payload["arbitrage"]
    else:
        db.add(SelectionAnalysis(
            keyword=keyword,
            scored_at=now,
            xianyu_payload=payload["xianyu"],
            pdd_payload=payload["pdd"],
            arbitrage=payload["arbitrage"],
        ))
    await db.commit()
    payload["scored_at"] = now.isoformat()
    payload["cached"] = False
    return payload


@router.get("/ten-dim/keywords", summary="十维度选品候选关键词（今日两池有数据的）")
async def ten_dim_keywords(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    day_start = _cn_day_start()

    pdd_rows = (await db.execute(
        select(PddSearchRun.keyword_text, func.max(PddSearchRun.created_at))
        .where(PddSearchRun.created_at >= day_start)
        .group_by(PddSearchRun.keyword_text)
    )).all()
    pdd_map = {kw: ts for kw, ts in pdd_rows if kw}

    xy_rows = (await db.execute(
        select(Product.category)
        .where(Product.source_platform == Platform.XIANYU)
        .where(Product.is_active == True)
        .where(Product.category.isnot(None))
        .distinct()
    )).scalars().all()
    xy_set = {c for c in xy_rows if c}

    cache_rows = (await db.execute(select(SelectionAnalysis))).scalars().all()
    cache_map = {c.keyword: c.scored_at for c in cache_rows}

    all_kw = sorted(set(pdd_map) | xy_set)
    items = []
    for kw in all_kw:
        has_pdd = kw in pdd_map
        has_xianyu = kw in xy_set
        scored_at = cache_map.get(kw)
        latest_pdd = pdd_map.get(kw)
        # 缓存比最新一次 PDD 采集还旧 = 过期，建议重新分析
        stale = bool(scored_at and latest_pdd and scored_at < latest_pdd)
        items.append({
            "keyword": kw,
            "has_pdd": has_pdd,
            "has_xianyu": has_xianyu,
            "both": has_pdd and has_xianyu,
            "cached": scored_at is not None,
            "scored_at": scored_at.isoformat() if scored_at else None,
            "stale": stale,
        })
    # 两池齐全的排前面
    items.sort(key=lambda x: (not x["both"], x["keyword"]))
    return {"total": len(items), "items": items}


@router.get("/ten-dim/{keyword}", summary="某关键词的十维度分析（有缓存直接返回）")
async def ten_dim_get(
    keyword: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    existing = (await db.execute(
        select(SelectionAnalysis).where(SelectionAnalysis.keyword == keyword)
    )).scalar_one_or_none()
    if existing:
        return {
            "keyword": keyword,
            "scored_at": existing.scored_at.isoformat() if existing.scored_at else None,
            "cached": True,
            "xianyu": existing.xianyu_payload,
            "pdd": existing.pdd_payload,
            "arbitrage": existing.arbitrage,
        }
    return await _compute_and_cache(db, keyword)


@router.post("/ten-dim/{keyword}/refresh", summary="重新分析（强制重算覆盖缓存）")
async def ten_dim_refresh(
    keyword: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await _compute_and_cache(db, keyword)


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
