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
from sqlalchemy import func, select, text, update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.selection import Category, Keyword, PddCategoryAccount
from app.models.system import Account, User

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
        "category_slug": k.category.slug if k.category else None,
        "pdd_mode": k.pdd_mode,
        "pdd_safe": k.pdd_safe,
        "schedule_enabled": k.schedule_enabled,
        "is_active": k.is_active,
        "pdd_last_searched_at": k.pdd_last_searched_at.isoformat() if k.pdd_last_searched_at else None,
        "pdd_last_status": k.pdd_last_status,
        "pdd_searches_total": k.pdd_searches_total,
        "xianyu_safe": k.xianyu_safe,
        "xianyu_last_searched_at": k.xianyu_last_searched_at.isoformat() if k.xianyu_last_searched_at else None,
        "xianyu_last_status": k.xianyu_last_status,
        "xianyu_searches_total": k.xianyu_searches_total,
    }


# ── PDD 采集号（accounts.platform='pdd_crawler'）────────────────
@router.get("/accounts", summary="PDD 采集号列表（给品类分配用，只列在用的号）")
async def list_pdd_accounts(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    # 只列在用的号（is_active=true）——停用/备用号（如未绑机的 pdd_crawler_2117/
    # _4310/_5514）不出现在分配下拉里。按 SOP 加手机绑号会置 is_active=true，自动复现。
    stmt = (
        select(Account)
        .where(Account.platform == "pdd_crawler")
        .where(Account.is_active.is_(True))
        .order_by(Account.account_name)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(a.id),
            "account_name": a.account_name,
            "bound_device_serial": a.bound_device_serial,
            "is_active": a.is_active,
        }
        for a in rows
    ]


# ── 分类 ──────────────────────────────────────────────────────
@router.get("/categories", summary="分类列表（含 PDD 词数 + 分配的采集号）")
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

    # 一次查出所有品类的号绑定，避免 N+1
    assign_rows = (
        await db.execute(
            select(PddCategoryAccount.category_id, PddCategoryAccount.account_id)
        )
    ).all()
    by_cat: dict[str, list[str]] = {}
    for cat_id, acct_id in assign_rows:
        by_cat.setdefault(str(cat_id), []).append(str(acct_id))

    return [
        {
            "id": str(c.id),
            "name": c.name,
            "slug": c.slug,
            "is_active": c.is_active,
            "keyword_count": cnt,
            "account_ids": by_cat.get(str(c.id), []),
        }
        for c, cnt in rows
    ]


class CategoryAccountsBody(BaseModel):
    account_ids: list[str] = Field(
        default_factory=list,
        description="该品类分配给哪些采集号（accounts.id）。空 = 未分配 = 不采集。",
    )


@router.put("/categories/{cat_id}/accounts", summary="设置品类分配的采集号（整组覆盖）")
async def set_category_accounts(
    cat_id: str,
    body: CategoryAccountsBody,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cat = (await db.execute(select(Category).where(Category.id == cat_id))).scalar_one_or_none()
    if cat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="分类不存在")

    wanted = list(dict.fromkeys(body.account_ids))  # 去重保序
    if wanted:
        valid = (
            await db.execute(
                select(Account.id).where(
                    Account.id.in_(wanted), Account.platform == "pdd_crawler"
                )
            )
        ).scalars().all()
        valid_set = {str(v) for v in valid}
        bad = [a for a in wanted if a not in valid_set]
        if bad:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"以下不是有效的 PDD 采集号：{bad}",
            )

    # 整组覆盖：先删旧绑定，再插新的
    await db.execute(
        PddCategoryAccount.__table__.delete().where(
            PddCategoryAccount.category_id == cat_id
        )
    )
    for acct_id in wanted:
        db.add(PddCategoryAccount(category_id=cat_id, account_id=acct_id))
    await db.commit()
    return {"ok": True, "category_id": cat_id, "account_ids": wanted}


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
    xianyu_safe: bool = True


class KeywordUpdate(BaseModel):
    text: str | None = Field(None, min_length=1, max_length=128)
    category_id: str | None = None
    pdd_mode: str | None = None
    pdd_safe: bool | None = None
    schedule_enabled: bool | None = None
    is_active: bool | None = None
    xianyu_safe: bool | None = None


async def _get_keyword(db: AsyncSession, kid: str) -> Keyword:
    stmt = select(Keyword).options(selectinload(Keyword.category)).where(Keyword.id == kid)
    k = (await db.execute(stmt)).scalar_one_or_none()
    if k is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="词不存在")
    return k


class BulkToggleBody(BaseModel):
    """按当前筛选范围（分类 + 搜索词）批量开关某个字段。"""

    field: str = Field(..., description="pdd_safe / xianyu_safe / schedule_enabled")
    value: bool
    category_id: str | None = None
    q: str | None = None


@router.post("/bulk-toggle", summary="按筛选范围批量开关 pdd_safe/xianyu_safe/schedule_enabled（跨页）")
async def bulk_toggle(
    body: BulkToggleBody,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    if body.field not in ("pdd_safe", "xianyu_safe", "schedule_enabled"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="不支持的字段")
    conds = [_PDD_FILTER]
    if body.category_id:
        conds.append(Keyword.category_id == body.category_id)
    if body.q:
        conds.append(Keyword.text.ilike(f"%{body.q}%"))
    stmt = sa_update(Keyword).values(**{body.field: body.value})
    for c in conds:
        stmt = stmt.where(c)
    res = await db.execute(stmt)
    await db.commit()
    return {"ok": True, "updated": res.rowcount or 0, "field": body.field, "value": body.value}


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
        xianyu_safe=body.xianyu_safe,
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
    if body.xianyu_safe is not None:
        k.xianyu_safe = body.xianyu_safe
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
