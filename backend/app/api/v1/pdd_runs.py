"""PDD 采集任务历史 —— 前端 Ops 看板 API。

读 pdd_search_runs（任务历史落库），给前端 Ops 面板出看板聚合 + 流水。

  GET  /api/v1/pdd-runs/summary   看板聚合（今日计数/成功率/趋势/最近/风控 + worker 在线）
  GET  /api/v1/pdd-runs/          分页流水（支持 status/source/keyword 过滤）

鉴权：登录用户（get_current_user）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.system import User
from app.services.pdd_app_queue import get_worker_status
from app.services.pdd_search_run import list_runs, summary

router = APIRouter()


@router.get("/summary", summary="Ops 看板聚合")
async def read_summary(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    data = await summary(db)
    data["worker"] = await get_worker_status()
    return data


@router.get("/", summary="任务历史流水（分页）")
async def read_runs(
    status: str | None = Query(None, description="过滤状态：ok/empty/partial/failed/risk_blocked/timeout"),
    source: str | None = Query(None, description="过滤来源：lib/selection/manual/emergency"),
    keyword: str | None = Query(None, description="关键词模糊匹配"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    return await list_runs(
        db, status=status, source=source, keyword=keyword,
        limit=limit, offset=offset,
    )
