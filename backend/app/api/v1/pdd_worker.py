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

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from app.core.config import get_settings
from app.services.pdd_app_queue import (
    PddAppResult,
    get_worker_status,
    pop_task,
    push_result,
    record_worker_heartbeat,
)

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
    _: None = Depends(verify_worker_token),
):
    """长轮询：有任务立刻返回，没任务阻塞最多 wait_s 秒。

    返回 ``{"task": null}`` 表示队列空了请重试。
    """
    task = await pop_task(timeout_s=wait_s)
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
    """worker 跑完任务后 POST 回来。"""
    await push_result(result)
    return {"ok": True, "task_id": result.task_id}


@router.post(
    "/heartbeat",
    summary="worker 心跳上报",
    response_model=None,
)
async def post_heartbeat(
    devices: list[str],
    _: None = Depends(verify_worker_token),
):
    """worker 每 30-60s 上报一次：'我在线，连接了 [手机1, 手机2, ...]'"""
    await record_worker_heartbeat(devices)
    return {"ok": True}


@router.get(
    "/status",
    summary="管理面查 worker 在不在",
    response_model=None,
)
async def get_status(_: None = Depends(verify_worker_token)):
    """供管理面或自检脚本调用：返回 worker 当前是否在线 + 连接的设备列表。"""
    return await get_worker_status()
