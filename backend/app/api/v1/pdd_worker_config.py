"""PDD worker 调度参数 —— 前端管理 API。

前端 Ops 面板用这组 endpoint 读/改 worker 的拟人化调度参数，改完写进
SystemConfig，home worker 下个心跳周期（≤45s）自动拉取热更新，无需重启
worker、无需远程桌面改 .env。

  GET  /api/v1/pdd-worker-config/        当前完整配置（默认 + DB 覆盖）
  GET  /api/v1/pdd-worker-config/specs   表单元数据（范围/标签/默认/分组）
  PUT  /api/v1/pdd-worker-config/         提交 patch（可只含部分字段）

鉴权：登录用户（get_current_user），与系统其他设置一致。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.system import User
from app.services.pdd_worker_config import (
    get_runtime_config,
    specs_for_frontend,
    update_runtime_config,
)

router = APIRouter()


@router.get("/", summary="获取 PDD worker 当前调度配置")
async def read_config(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    return await get_runtime_config(db)


@router.get("/specs", summary="获取参数表单元数据（范围/标签/默认）")
async def read_specs(
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    return specs_for_frontend()


class ConfigPatch(BaseModel):
    """部分更新。只传要改的字段，未传的保持原值。"""

    patch: dict[str, Any] = Field(
        ...,
        description="参数名→新值的映射，例如 {\"humanize_pace\": 0.7}",
    )


@router.put("/", summary="更新 PDD worker 调度配置")
async def write_config(
    body: ConfigPatch,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    if not body.patch:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="patch 为空，没有要更新的字段",
        )
    try:
        merged = await update_runtime_config(db, body.patch)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    return {
        "ok": True,
        "config": merged,
        "note": "已保存。home worker 将在下个心跳周期（≤45s）拉取并热更新。",
    }
