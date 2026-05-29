"""PDD 词库管理 —— 前端 CRUD API。

管理 selection_keywords 里「'pdd' ∈ target_platforms」的词：增删改、调
pdd_mode / pdd_safe / schedule_enabled，并能看到每个词的 PDD 跑动状态
（上次跑的时间/结果/累计次数）。fire_from_lib 就是按这些字段选词派任务的。

  GET    /api/v1/pdd-keywords/categories   分类列表（含词数）
  POST   /api/v1/pdd-keywords/categories   新建分类
  GET    /api/v1/pdd-keywords/             词列表（分类/关键词/安全词 过滤 + 分页）
  POST   /api/v1/pdd-keywords/             新建词（自动带上 'pdd' 平台）
  PUT    /api/v1/pdd-keywords/{id}         改词
  DELETE /api/v1/pdd-keywords/{id}         删词

鉴权：登录用户（get_current_user）。
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.selection import Category, Keyword
from app.models.system import User

router = APIRouter()

# worker 支持的采集模式（fast/list_deep 已实装，detail_* 为 Phase 2 占位）
VALID_MODES = ("fast", "list_deep", "detail_smart", "detail_deep")

# 'pdd' ∈ target_platforms（JSON 列，需 ::jsonb 才能用 @>）
_PDD_FILTER = text("selection_keywords.target_platforms::jsonb @> '[\"pdd\"]'::jsonb")


def _keyword_out(k: Keyword) -> dict[str, Any]:
    return {
        "id": str(k.id),
        "text": k.text,
        "category_id": str(k.category_id),
        "category_name": k.category.name if k.category else None,
        "pdd_mode": k.pdd_mode,
        "pdd_safe": k.pdd_safe,
        "schedule_enabled": k.schedule_enabled,
        "is_active": k.is_active,
        "pdd_last_searched_at": k.pdd_last_searched_at.isoformat() if k.pdd_last_searched_at else None,
        "pdd_last_status": k.pdd_last_status,
        "pdd_searches_total": k.pdd_searches_total,
    }


# ── 分类 ──────────────────────────────────────────────────────
@router.get("/categories", summary="分类列表（含 PDD 词数）")
async def list_categories(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    stmt = (
        select(Category, func.count(Keyword.id))
        .outerjoin(Keyword, Keyword.category_id == Category.id)
        .group_by(Category.id)
        .order_by(Category.display_order, Category.name)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "id": str(c.id),
            "name": c.name,
            "slug": c.slug,
            "is_active": c.is_active,
            "keyword_count": cnt,
        }
        for c, cnt in rows
    ]


class CategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    slug: str | None = Field(None, max_length=64, description="留空则按 name 生成")


@router.post("/categories", summary="新建分类")
async def create_category(
    body: CategoryCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    slug = (body.slug or "").strip()
    if not slug:
        # 简单 slug 化：非字母数字转连字符；中文场景下退化为时间无意义，故保底用 name
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", body.name.strip().lower()).strip("-") or body.name.strip()
    cat = Category(name=body.name.strip(), slug=slug)
    db.add(cat)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="分类 slug 已存在")
    await db.refresh(cat)
    return {"id": str(cat.id), "name": cat.name, "slug": cat.slug}


# ── 词 ────────────────────────────────────────────────────────
@router.get("/", summary="PDD 词列表（分页）")
async def list_keywords(
    category_id: str | None = Query(None),
    q: str | None = Query(None, description="关键词模糊匹配"),
    pdd_safe: bool | None = Query(None, description="只看安全/禁用词"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    conds = [_PDD_FILTER]
    if category_id:
        conds.append(Keyword.category_id == category_id)
    if q:
        conds.append(Keyword.text.ilike(f"%{q}%"))
    if pdd_safe is not None:
        conds.append(Keyword.pdd_safe.is_(pdd_safe))

    count_stmt = select(func.count()).select_from(Keyword)
    list_stmt = select(Keyword).options(selectinload(Keyword.category))
    for c in conds:
        count_stmt = count_stmt.where(c)
        list_stmt = list_stmt.where(c)

    total = (await db.execute(count_stmt)).scalar_one()
    list_stmt = (
        list_stmt.order_by(
            Keyword.pdd_last_searched_at.asc().nullsfirst(),
            Keyword.text,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(list_stmt)).scalars().all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_keyword_out(k) for k in rows],
    }


class KeywordCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=128)
    category_id: str
    pdd_mode: str = Field("fast")
    pdd_safe: bool = True
    schedule_enabled: bool = True


class KeywordUpdate(BaseModel):
    text: str | None = Field(None, min_length=1, max_length=128)
    category_id: str | None = None
    pdd_mode: str | None = None
    pdd_safe: bool | None = None
    schedule_enabled: bool | None = None
    is_active: bool | None = None


async def _get_keyword(db: AsyncSession, kid: str) -> Keyword:
    stmt = select(Keyword).options(selectinload(Keyword.category)).where(Keyword.id == kid)
    k = (await db.execute(stmt)).scalar_one_or_none()
    if k is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="词不存在")
    return k


@router.post("/", summary="新建 PDD 词")
async def create_keyword(
    body: KeywordCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    if body.pdd_mode not in VALID_MODES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"pdd_mode 必须是 {VALID_MODES}")
    cat = (await db.execute(select(Category).where(Category.id == body.category_id))).scalar_one_or_none()
    if cat is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="分类不存在")
    k = Keyword(
        text=body.text.strip(),
        category_id=body.category_id,
        target_platforms=["pdd"],
        pdd_mode=body.pdd_mode,
        pdd_safe=body.pdd_safe,
        schedule_enabled=body.schedule_enabled,
    )
    db.add(k)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="该分类下已存在同名词")
    k = await _get_keyword(db, str(k.id))
    return _keyword_out(k)


@router.put("/{kid}", summary="修改 PDD 词")
async def update_keyword(
    kid: str,
    body: KeywordUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    k = await _get_keyword(db, kid)
    if body.pdd_mode is not None:
        if body.pdd_mode not in VALID_MODES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"pdd_mode 必须是 {VALID_MODES}")
        k.pdd_mode = body.pdd_mode
    if body.text is not None:
        k.text = body.text.strip()
    if body.category_id is not None:
        cat = (await db.execute(select(Category).where(Category.id == body.category_id))).scalar_one_or_none()
        if cat is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="分类不存在")
        k.category_id = body.category_id
    if body.pdd_safe is not None:
        k.pdd_safe = body.pdd_safe
    if body.schedule_enabled is not None:
        k.schedule_enabled = body.schedule_enabled
    if body.is_active is not None:
        k.is_active = body.is_active
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="该分类下已存在同名词")
    k = await _get_keyword(db, kid)
    return _keyword_out(k)


@router.delete("/{kid}", summary="删除 PDD 词")
async def delete_keyword(
    kid: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    k = await _get_keyword(db, kid)
    await db.delete(k)
    await db.commit()
    return {"ok": True, "id": kid}
