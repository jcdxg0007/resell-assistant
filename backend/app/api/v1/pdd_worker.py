"""HTTP bridge for the home-Windows PDD APP worker.

家里 Windows worker 不能直连 Sealos K8s 内部 Redis（ClusterIP），所以走
HTTPS + Sealos Ingress + Bearer token 三个 endpoint：

  GET  /api/v1/pdd-worker/poll          长轮询拉任务（阻塞 ≤ wait_s 秒）
  POST /api/v1/pdd-worker/result        推一个 task 的结果
  POST /api/v1/pdd-worker/heartbeat     上报"我在线 + 连了哪些手机"

鉴权：Authorization: Bearer <PDD_WORKER_TOKEN>。token 在 backend .env 里配，
worker 端 .env 里填同一个值。Token 错误返回 401。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from app.core.config import get_settings
from app.core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.pdd_app_queue import (
    PddAppResult,
    get_task_meta,
    get_worker_status,
    pop_task,
    push_result,
    record_worker_heartbeat,
)
from app.services.pdd_worker_config import get_runtime_config

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()


def verify_worker_token(authorization: str | None = Header(None)) -> None:
    """验证 worker 的 Bearer token。"""
    if settings.PDD_WORKER_TOKEN in ("", "change-me-pdd-worker-token"):
        # 启动期还没改 token，硬性拒绝避免接出去裸奔
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pdd_worker_token_not_configured",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing_bearer")
    token = authorization.split(" ", 1)[1].strip()
    if token != settings.PDD_WORKER_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad_token")


@router.get(
    "/poll",
    summary="worker 拉任务（长轮询）",
    response_model=None,
)
async def poll_task(
    wait_s: int = Query(25, ge=1, le=60, description="最长阻塞秒数（不要 > 60，HTTP 超时）"),
    account: str | None = Query(None, description="本 worker 绑定的采集号 BOUND_PDD_ACCOUNT；多号路由用"),
    _: None = Depends(verify_worker_token),
):
    """长轮询：有任务立刻返回，没任务阻塞最多 wait_s 秒。

    account 给定 → 只取该号专属队列 + 默认队列（多号品类隔离，roadmap §15）；
    为空（旧 worker / 未配号）→ 只取默认队列，行为不变。

    返回 ``{"task": null}`` 表示队列空了请重试。
    """
    task = await pop_task(timeout_s=wait_s, account=account)
    if task is None:
        return {"task": None}
    return {"task": task.model_dump()}


@router.post(
    "/result",
    summary="worker 推结果",
    response_model=None,
)
async def post_result(
    result: PddAppResult,
    _: None = Depends(verify_worker_token),
):
    """worker 跑完任务后 POST 回来。

    两件事：
    1. push_result：放进 Redis 结果队列（手动派发的同步 await_result 仍能拿到）。
    2. 即时落库：读 task-meta 补全关键词信息，直接写 pdd_search_runs + 回写词库。
       这条路不依赖那个易被 celery 槽位/重启搞丢的 await-persist 任务，是落库
       的主路径；await-persist 退化为兜底（幂等锁去重，谁先落谁算）。
    """
    await push_result(result)
    try:
        meta = await get_task_meta(result.task_id)
        if meta is not None:
            from app.services.pdd_autobatch import persist_pdd_result
            await persist_pdd_result(result, meta)
    except Exception as exc:  # noqa: BLE001 — 落库失败不影响给 worker 回 ack
        logger.warning(f"post_result 即时落库失败 task_id={result.task_id}: {exc}")
    return {"ok": True, "task_id": result.task_id}


@router.post(
    "/heartbeat",
    summary="worker 心跳上报",
    response_model=None,
)
async def post_heartbeat(
    body: list[str] | dict[str, Any],
    _: None = Depends(verify_worker_token),
):
    """worker 每 30-60s 上报一次：'我在线，连接了 [手机1, 手机2, ...]'

    兼容两种 body：
    - 旧：纯列表 ["serial1", ...]
    - 新：{"devices": [...], "scheduler": {burst 快照}, "worker": "名字", "account": "号"}
      scheduler 用于精确预估 ETA；worker 用于多 worker 的 per-worker 心跳 key；
      account 用于多号路由（beat tick 据此知道哪些号在线，roadmap §15）。
    """
    if isinstance(body, list):
        devices, scheduler, worker_name, account = body, None, None, None
    else:
        devices = body.get("devices") or []
        scheduler = body.get("scheduler")
        worker_name = body.get("worker")
        account = body.get("account")
    await record_worker_heartbeat(
        devices, scheduler=scheduler, worker_name=worker_name, account=account
    )
    return {"ok": True}


@router.get(
    "/status",
    summary="管理面查 worker 在不在",
    response_model=None,
)
async def get_status(_: None = Depends(verify_worker_token)):
    """供管理面或自检脚本调用：返回 worker 当前是否在线 + 连接的设备列表。"""
    return await get_worker_status()


@router.get(
    "/runtime-config",
    summary="worker 拉运行时调度配置",
    response_model=None,
)
async def get_runtime_config_for_worker(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_worker_token),
):
    """worker 每个心跳周期拉一次：返回完整调度配置（DB 覆盖项盖在默认值上）。

    DB 里从没改过 → 返回纯默认值，worker 行为与改造前一致。
    """
    return await get_runtime_config(db)
