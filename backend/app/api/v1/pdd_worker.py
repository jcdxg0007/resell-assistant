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
from pydantic import BaseModel

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


class LogisticsRunReport(BaseModel):
    """worker 上报一次「查快递」拟人动作（roadmap §11.4）。"""
    trigger: str           # A = burst 结尾 / B = inter-burst 静默期
    status: str            # viewed / empty / nav_failed
    account_name: str | None = None
    device_serial: str | None = None
    elapsed_ms: int | None = None
    note: str | None = None


@router.post(
    "/logistics",
    summary="worker 上报查快递事件",
    response_model=None,
)
async def post_logistics(
    report: LogisticsRunReport,
    _: None = Depends(verify_worker_token),
):
    """查快递不是派发任务、无 task_id，单独一个轻量上报口落到 logistics_runs，
    供「任务记录」合并展示。落库失败不影响 worker（这里也只返回 ok）。"""
    try:
        from app.services.logistics_run import persist_logistics_run
        await persist_logistics_run(
            trigger=report.trigger,
            status=report.status,
            account_name=report.account_name,
            device_serial=report.device_serial,
            elapsed_ms=report.elapsed_ms,
            note=report.note,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"post_logistics 落库失败: {exc}")
    return {"ok": True}


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


# ── 管家(supervisor)控制面 ───────────────────────────────────────
# 家里那个常驻的管家进程用这两个口：拉命令 + 上报状态。前端侧的「下命令 / 读状态」
# 在 pdd_runs.py（登录用户鉴权），两边经 Redis 命令队列 + 状态快照解耦。
@router.get(
    "/control/poll",
    summary="管家拉控制命令 + serial↔账号映射",
    response_model=None,
)
async def supervisor_poll(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_worker_token),
):
    """管家每隔几秒拉一次：返回待执行命令 + 每台手机该绑的采集号。

    bindings 让管家「启动某台手机」时自动带上正确账号，免去本地编辑 .bat。
    """
    from app.services.pdd_worker_control import (
        get_pdd_device_bindings, pop_supervisor_commands,
    )
    commands = await pop_supervisor_commands()
    bindings = await get_pdd_device_bindings(db)
    return {"commands": commands, "bindings": bindings}


@router.post(
    "/control/status",
    summary="管家上报状态快照",
    response_model=None,
)
async def supervisor_status(
    body: dict[str, Any],
    _: None = Depends(verify_worker_token),
):
    """管家把最新状态（设备/子进程/git commit/最近命令结果）POST 回来存 Redis。"""
    from app.services.pdd_worker_control import set_supervisor_status
    await set_supervisor_status(body)
    return {"ok": True}
