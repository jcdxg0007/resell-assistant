"""
System settings API — read/write SystemConfig entries.
Provides a clean interface for frontend to toggle features like auto-purchase mode.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.system import User, SystemConfig

router = APIRouter()

DEFAULTS = {
    "auto_purchase_mode": "manual",
    "dingtalk_webhook_url": "",
    "dingtalk_secret": "",
}


async def _get_config(db: AsyncSession, key: str) -> str:
    result = await db.execute(select(SystemConfig).where(SystemConfig.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else DEFAULTS.get(key, "")


async def _set_config(db: AsyncSession, key: str, value: str, description: str = ""):
    result = await db.execute(select(SystemConfig).where(SystemConfig.key == key))
    row = result.scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(SystemConfig(key=key, value=value, description=description, value_type="string"))
    await db.commit()


@router.get("/", summary="获取所有设置")
async def get_all_settings(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(SystemConfig))
    rows = result.scalars().all()
    config = {**DEFAULTS}
    for row in rows:
        config[row.key] = row.value
    return config


class SettingUpdate(BaseModel):
    key: str
    value: str


@router.put("/", summary="更新设置")
async def update_setting(
    req: SettingUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await _set_config(db, req.key, req.value)
    return {"message": f"设置 {req.key} 已更新"}


@router.get("/auto-purchase-mode", summary="获取采购模式")
async def get_auto_purchase_mode(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    mode = await _get_config(db, "auto_purchase_mode")
    return {"mode": mode}
