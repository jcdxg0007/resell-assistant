"""PDD 采集任务历史 —— 前端 Ops 看板 API。

读 pdd_search_runs（任务历史落库），给前端 Ops 面板出看板聚合 + 流水。

  GET  /api/v1/pdd-runs/summary   看板聚合（今日计数/成功率/趋势/最近/风控 + worker 在线）
  GET  /api/v1/pdd-runs/          分页流水（支持 status/source/keyword 过滤）

鉴权：登录用户（get_current_user）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.system import User
from app.services.pdd_app_queue import (
    PddAppTask, await_result, enqueue_task, get_worker_status,
)
from app.services.pdd_search_run import list_runs, persist_search_run, summary

logger = logging.getLogger(__name__)
router = APIRouter()

# 紧急派发用：priority=9 让 worker LPUSH 插队 + 跳过 inter-burst 静默期
_DISPATCH_PRIORITY = 9
# 后台 await 任务的强引用集合，防止 create_task 出来的协程被 GC 提前回收
_bg_tasks: set[asyncio.Task] = set()


@router.get("/summary", summary="Ops 看板聚合")
async def read_summary(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    data = await summary(db)
    data["worker"] = await get_worker_status()
    return data


class DispatchBody(BaseModel):
    """手动派发一个 PDD 搜索任务。"""

    keyword: str = Field(..., min_length=1, max_length=128)
    mode: str = Field("fast", description="fast / deep")


async def _await_and_persist(task_id: str, keyword: str, mode: str, timeout_s: int) -> None:
    """后台等 worker 把结果推回来，再落到 pdd_search_runs（source=manual）。

    跑在 FastAPI 事件循环里、不阻塞派发请求的响应。worker 离线/慢/静默都
    由 await_result 超时兜底（落 timeout 行），绝不卡住进程。
    """
    try:
        result = await await_result(task_id, timeout_s=timeout_s)
        if result is None:
            await persist_search_run(
                status="timeout", keyword_text=keyword, task_id=task_id,
                source="manual", mode=mode, priority=_DISPATCH_PRIORITY,
            )
            return
        items = result.items or []
        prices = sorted(float(it["price"]) for it in items if it.get("price"))
        p_min = prices[0] if prices else None
        p_median = prices[len(prices) // 2] if prices else None
        bucket = result.status
        if bucket == "ok" and not items:
            bucket = "empty"
        await persist_search_run(
            status=bucket, keyword_text=keyword, task_id=task_id, source="manual",
            mode=mode, items_count=len(items), price_min=p_min, price_median=p_median,
            risk_signals=result.risk_signals, device_serial=result.device_serial,
            account_name=result.account_name, elapsed_ms=result.elapsed_ms,
            priority=_DISPATCH_PRIORITY, error=result.error,
        )
    except Exception as exc:  # noqa: BLE001 — 后台任务异常只记日志
        logger.warning(f"pdd dispatch await/persist failed (kw='{keyword}'): {exc}")


@router.post("/dispatch", summary="手动派发一个 PDD 搜索任务（紧急插队）")
async def dispatch_search(
    body: DispatchBody,
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """前端「PDD搜索 / 同时搜」用：紧急派一个 PDD search 任务（插队 + 跳静默）。

    立即返回 task_id（不阻塞），后台协程负责 await 结果并落库。前端稍后刷新
    «拼多多采集结果» 即可看到。worker 离线直接 503，让前端给出明确提示。
    """
    wstatus = await get_worker_status()
    if not wstatus.get("online"):
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pdd_worker_offline",
        )
    mode = "deep" if body.mode == "deep" else "fast"
    task_timeout = 180 if mode == "deep" else 90
    task = PddAppTask(
        kind="search",
        payload={"keyword": body.keyword.strip(), "mode": mode},
        priority=_DISPATCH_PRIORITY,
        timeout_s=task_timeout,
    )
    await enqueue_task(task)
    bg = asyncio.create_task(
        _await_and_persist(task.task_id, body.keyword.strip(), mode, task_timeout + 60)
    )
    _bg_tasks.add(bg)
    bg.add_done_callback(_bg_tasks.discard)
    logger.info(f"pdd dispatch: task_id={task.task_id} keyword='{body.keyword}' mode={mode}")
    return {"ok": True, "task_id": task.task_id, "keyword": body.keyword.strip(), "mode": mode}


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
