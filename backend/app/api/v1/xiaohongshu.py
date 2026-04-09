from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.product import Product, ProductScore
from app.models.xiaohongshu import (
    XhsNote, XhsNoteAnalytics, XhsHotTopic, XhsTrendingKeyword,
    XhsCompetitorNote, XhsContentTemplate,
)
from app.models.system import User
from app.services.xiaohongshu.scoring import (
    XhsScoringInput, calculate_xhs_score, XHS_DECISION_LABELS,
)

router = APIRouter()


# ─── Hot Topics & Trending ───


@router.get("/trending/topics", summary="热门话题")
async def trending_topics(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(XhsHotTopic)
        .where(XhsHotTopic.is_trending == True)
        .order_by(XhsHotTopic.growth_rate_daily.desc().nulls_last())
        .limit(limit)
    )
    topics = result.scalars().all()
    return {
        "items": [
            {
                "id": str(t.id),
                "topic_name": t.topic_name,
                "category": t.category,
                "view_count": t.view_count,
                "note_count": t.note_count,
                "growth_rate_daily": t.growth_rate_daily,
                "growth_rate_weekly": t.growth_rate_weekly,
                "captured_at": t.captured_at.isoformat(),
            }
            for t in topics
        ],
    }


@router.get("/trending/keywords", summary="飙升关键词")
async def trending_keywords(
    source: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(XhsTrendingKeyword).order_by(XhsTrendingKeyword.growth_rate.desc().nulls_last())
    if source:
        query = query.where(XhsTrendingKeyword.source == source)
    query = query.limit(limit)
    result = await db.execute(query)
    keywords = result.scalars().all()
    return {
        "items": [
            {
                "keyword": k.keyword,
                "source": k.source,
                "search_volume": k.search_volume_estimated,
                "growth_rate": k.growth_rate,
                "has_supply": k.related_products_found,
                "captured_at": k.captured_at.isoformat(),
            }
            for k in keywords
        ],
    }


# ─── Competitor Analysis ───


@router.get("/competitors", summary="竞品笔记分析")
async def competitor_notes(
    keyword: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = (
        select(XhsCompetitorNote)
        .where(XhsCompetitorNote.keyword == keyword)
        .order_by(XhsCompetitorNote.likes.desc())
    )
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.offset((page - 1) * page_size).limit(page_size)
    notes = (await db.execute(query)).scalars().all()

    # Aggregated stats
    agg_q = select(
        func.avg(XhsCompetitorNote.likes),
        func.avg(XhsCompetitorNote.collects),
        func.avg(XhsCompetitorNote.comments),
        func.avg(XhsCompetitorNote.interaction_rate),
        func.avg(XhsCompetitorNote.purchase_intent_ratio),
    ).where(XhsCompetitorNote.keyword == keyword)
    agg = (await db.execute(agg_q)).one_or_none()

    return {
        "total": total,
        "aggregated": {
            "avg_likes": round(agg[0] or 0, 1),
            "avg_collects": round(agg[1] or 0, 1),
            "avg_comments": round(agg[2] or 0, 1),
            "avg_interaction_rate": round(agg[3] or 0, 2),
            "avg_purchase_intent": round(agg[4] or 0, 1),
        },
        "items": [
            {
                "id": str(n.id),
                "xhs_note_id": n.xhs_note_id,
                "title": n.title,
                "likes": n.likes,
                "collects": n.collects,
                "comments": n.comments,
                "interaction_rate": n.interaction_rate,
                "has_product_link": n.has_product_link,
                "purchase_intent_ratio": n.purchase_intent_ratio,
                "cover_style": n.cover_style,
                "content_structure": n.content_structure,
            }
            for n in notes
        ],
    }


# ─── XHS Scoring ───


class XhsScoreRequest(BaseModel):
    topic_view_count: int = 0
    note_growth_30d_pct: float = 0.0
    photogenic_level: int = 3
    content_form_count: int = 2
    source_review_photo_quality: int = 2
    category_interaction_rate: float = 0.0
    purchase_intent_ratio: float = 0.0
    new_notes_30d: int = 0
    sales_notes_30d: int = 0
    profit_margin_pct: float = 0.0


@router.post("/score/{product_id}", summary="触发小红书五维度评分")
async def score_product_xhs(
    product_id: str,
    req: XhsScoreRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="商品不存在")

    scoring_input = XhsScoringInput(
        topic_view_count=req.topic_view_count,
        note_growth_30d_pct=req.note_growth_30d_pct,
        photogenic_level=req.photogenic_level,
        content_form_count=req.content_form_count,
        source_review_photo_quality=req.source_review_photo_quality,
        category_interaction_rate=req.category_interaction_rate,
        purchase_intent_ratio=req.purchase_intent_ratio,
        new_notes_30d=req.new_notes_30d,
        sales_notes_30d=req.sales_notes_30d,
        profit_margin_pct=req.profit_margin_pct,
    )

    score_result = calculate_xhs_score(scoring_input)

    dim_dict = {d.name: {"score": d.score, "max": d.max_score, "label": d.label} for d in score_result.dimensions}
    db_score = ProductScore(
        product_id=product_id,
        score_type="xhs_5d",
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
            "decision_label": XHS_DECISION_LABELS.get(score_result.decision),
            "dimensions": dim_dict,
        },
    }


# ─── XHS Recommendations ───


@router.get("/recommendations", summary="小红书选品推荐")
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
            "product": {
                "id": str(product.id),
                "title": product.title,
                "price": product.price,
                "category": product.category,
                "image_urls": product.image_urls,
            },
            "score": {
                "total_score": score.total_score,
                "decision": score.decision,
                "decision_label": XHS_DECISION_LABELS.get(score.decision, score.decision),
                "dimensions": score.dimension_scores,
            },
        })

    return {"total": total, "page": page, "page_size": page_size, "items": items}


# ─── Content Templates ───


@router.get("/templates", summary="内容模板列表")
async def list_templates(
    template_type: str | None = None,
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(XhsContentTemplate).where(XhsContentTemplate.is_active == True)
    if template_type:
        query = query.where(XhsContentTemplate.template_type == template_type)
    if category:
        query = query.where(XhsContentTemplate.category == category)
    query = query.order_by(XhsContentTemplate.usage_count.desc())
    result = await db.execute(query)
    templates = result.scalars().all()

    return {
        "items": [
            {
                "id": str(t.id),
                "name": t.name,
                "template_type": t.template_type,
                "category": t.category,
                "content": t.content,
                "variables": t.variables,
                "usage_count": t.usage_count,
            }
            for t in templates
        ],
    }


# ─── Notes Management ───


@router.get("/notes", summary="笔记列表")
async def list_notes(
    status: str | None = None,
    account_id: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(XhsNote)
    if status:
        query = query.where(XhsNote.status == status)
    if account_id:
        query = query.where(XhsNote.account_id == account_id)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(XhsNote.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    notes = (await db.execute(query)).scalars().all()

    return {
        "total": total,
        "page": page,
        "items": [
            {
                "id": str(n.id),
                "title": n.title,
                "note_type": n.note_type,
                "content_type": n.content_type,
                "status": n.status,
                "tags": n.tags,
                "topics": n.topics,
                "published_at": n.published_at.isoformat() if n.published_at else None,
            }
            for n in notes
        ],
    }
