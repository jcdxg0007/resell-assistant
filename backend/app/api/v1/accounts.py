from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.system import User, Account

router = APIRouter()


class AccountCreate(BaseModel):
    platform: str
    account_name: str
    identity_group: str
    niche: str | None = None
    proxy_url: str | None = None
    user_agent: str | None = None
    viewport: dict | None = None


class AccountUpdate(BaseModel):
    proxy_url: str | None = None
    user_agent: str | None = None
    niche: str | None = None
    lifecycle_stage: str | None = None
    daily_publish_limit: int | None = None
    is_active: bool | None = None


@router.get("/", summary="账号列表")
async def list_accounts(
    platform: str | None = None,
    is_active: bool | None = True,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(Account)
    if platform:
        query = query.where(Account.platform == platform)
    if is_active is not None:
        query = query.where(Account.is_active == is_active)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(Account.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    accounts = (await db.execute(query)).scalars().all()

    return {
        "total": total,
        "page": page,
        "items": [_account_to_dict(a) for a in accounts],
    }


@router.post("/", summary="创建账号")
async def create_account(
    req: AccountCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    account = Account(
        platform=req.platform,
        account_name=req.account_name,
        identity_group=req.identity_group,
        niche=req.niche,
        proxy_url=req.proxy_url,
        user_agent=req.user_agent,
        viewport=req.viewport,
        lifecycle_stage="nurturing",
        daily_publish_limit=2,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return {"id": str(account.id), "message": "账号已创建"}


@router.get("/{account_id}", summary="账号详情")
async def get_account(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    return _account_to_dict(account)


@router.put("/{account_id}", summary="更新账号")
async def update_account(
    account_id: str,
    req: AccountUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(account, field, value)
    await db.commit()
    return {"message": "已更新"}


@router.post("/{account_id}/suspend", summary="暂停账号")
async def suspend_account(
    account_id: str,
    reason: str = "手动暂停",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    account.is_active = False
    account.lifecycle_stage = "suspended"
    account.suspended_reason = reason
    await db.commit()
    return {"message": "账号已暂停"}


@router.post("/{account_id}/activate", summary="激活账号")
async def activate_account(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    account.is_active = True
    if account.lifecycle_stage == "suspended":
        account.lifecycle_stage = "growing"
    account.suspended_reason = None
    await db.commit()
    return {"message": "账号已激活"}


@router.get("/stats/summary", summary="账号统计")
async def accounts_summary(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    platforms = ["xianyu", "xiaohongshu", "douyin"]
    platform_counts = {}
    for p in platforms:
        q = select(func.count()).where(Account.platform == p, Account.is_active == True)
        platform_counts[p] = (await db.execute(q)).scalar() or 0

    total_active = sum(platform_counts.values())
    suspended_q = select(func.count()).where(Account.is_active == False)
    suspended = (await db.execute(suspended_q)).scalar() or 0

    return {
        "total_active": total_active,
        "suspended": suspended,
        "by_platform": platform_counts,
    }


# ─── Platform Login ───────────────────────────────────────────

from app.services.platform_login import (
    start_login, poll_login_status, cancel_login, get_login_screenshot,
)


@router.post("/{account_id}/login", summary="发起平台扫码登录")
async def initiate_login(
    account_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    from app.services.browser import browser_manager
    if not browser_manager._browser:
        await browser_manager.start()

    account_config = {
        "proxy_url": account.proxy_url,
        "user_agent": account.user_agent,
        "viewport": account.viewport,
    }
    session = await start_login(str(account.id), account.platform, account_config)
    return {
        "status": session.status.value,
        "qr_image": session.qr_image_b64,
        "error": session.error,
        "platform": account.platform,
    }


@router.get("/{account_id}/login/status", summary="查询登录状态")
async def check_login_status(
    account_id: str,
    user: User = Depends(get_current_user),
):
    result = await poll_login_status(account_id)
    return result


@router.get("/{account_id}/login/screenshot", summary="获取登录页面截图")
async def login_screenshot(
    account_id: str,
    user: User = Depends(get_current_user),
):
    img = await get_login_screenshot(account_id)
    if not img:
        raise HTTPException(status_code=404, detail="无可用截图")
    return {"screenshot": img}


@router.post("/{account_id}/login/cancel", summary="取消登录")
async def cancel_login_flow(
    account_id: str,
    user: User = Depends(get_current_user),
):
    await cancel_login(account_id)
    return {"message": "已取消"}


def _account_to_dict(a: Account) -> dict:
    state_path = __import__("pathlib").Path(__file__).parent.parent.parent.parent / "playwright_states" / f"{a.id}.json"
    return {
        "id": str(a.id),
        "platform": a.platform,
        "account_name": a.account_name,
        "identity_group": a.identity_group,
        "niche": a.niche,
        "proxy_url": a.proxy_url,
        "lifecycle_stage": a.lifecycle_stage,
        "daily_publish_limit": a.daily_publish_limit,
        "daily_published_count": a.daily_published_count,
        "health_score": a.health_score,
        "is_active": a.is_active,
        "suspended_reason": a.suspended_reason,
        "last_active_at": a.last_active_at.isoformat() if a.last_active_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "logged_in": state_path.exists(),
    }
