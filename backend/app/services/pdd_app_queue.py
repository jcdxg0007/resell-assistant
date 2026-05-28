"""PDD APP worker 任务队列（K8s 内部 Redis 实现）。

架构：
- backend FastAPI 提供 HTTPS bridge（见 app/api/v1/pdd_worker.py）
- 家里 Windows worker 通过 HTTPS 长轮询 backend 拉任务、推结果
- 本模块仅服务端侧：把任务推到 Redis，把结果存到 Redis 供 caller 等待

数据契约见 docs/PDD-自建采集-roadmap.md §4.1。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.redis import redis_client

logger = logging.getLogger(__name__)

# Redis keys
TASK_QUEUE_KEY = "pdd_app:task_q"          # 任务队列（FIFO）
RESULT_KEY_PREFIX = "pdd_app:result:"      # 单任务结果（短 TTL）
WORKER_HEARTBEAT_KEY = "pdd_app:worker:heartbeat"  # worker 最近活跃时间

# 默认 TTL
RESULT_TTL_SECONDS = 600  # 结果在 Redis 里保留 10 分钟，超时未取走就丢弃

TaskKind = Literal["search", "detail", "history_price", "self_check"]


class PddAppTask(BaseModel):
    """worker 接到的任务（从 Redis 队列 LPOP 出来的内容）。"""

    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: TaskKind
    payload: dict[str, Any] = Field(default_factory=dict)
    account_id: str | None = None
    priority: int = 1
    timeout_s: int = 60
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class PddAppResult(BaseModel):
    """worker 跑完任务的结构化结果。"""

    task_id: str
    status: Literal["ok", "partial", "failed", "risk_blocked"]
    items: list[dict[str, Any]] = Field(default_factory=list)
    risk_signals: list[str] = Field(default_factory=list)
    device_serial: str | None = None
    account_name: str | None = None
    elapsed_ms: int | None = None
    error: str | None = None
    raw_screenshot_path: str | None = None  # worker 端本地截图路径，用于人工核查


# 紧急任务阈值：priority >= 这个值的任务会用 LPUSH 插到队首，并由 worker
# 端 scheduler 跳过 inter-burst quiet（5-30 min 静默期）。普通任务默认
# priority=1，紧急时显式传 ≥ 8。
EMERGENCY_PRIORITY_THRESHOLD = 8


async def enqueue_task(task: PddAppTask) -> None:
    """把任务推进 Redis 队列。worker 会 BLPOP 拉走。

    队列实现：
    - 普通任务（priority < EMERGENCY_PRIORITY_THRESHOLD）→ RPUSH 进队尾，FIFO
    - 紧急任务（priority ≥ EMERGENCY_PRIORITY_THRESHOLD）→ LPUSH 进队首，
      让 worker 下次 BLPOP 立刻拿到，跳过前面排队的普通任务

    注意：worker 端 scheduler 还会读 task.priority 决定是否跳 inter-burst
    quiet（拟人化节流）。两层配合才能真正实现"插队 + 立即开干"。
    """
    payload = task.model_dump_json()
    is_emergency = task.priority >= EMERGENCY_PRIORITY_THRESHOLD
    if is_emergency:
        await redis_client.lpush(TASK_QUEUE_KEY, payload)
    else:
        await redis_client.rpush(TASK_QUEUE_KEY, payload)
    logger.info(
        f"pdd_app_queue: enqueued task_id={task.task_id} "
        f"kind={task.kind} account={task.account_id} "
        f"priority={task.priority}{' [EMERGENCY/jump-queue]' if is_emergency else ''}"
    )


async def await_result(task_id: str, timeout_s: int = 120) -> PddAppResult | None:
    """阻塞等待 worker 把结果推回。

    用 BLPOP 实现：worker 完成后会 ``rpush`` 一份结果到 ``pdd_app:result:{task_id}``，
    backend 这里 BLPOP 一次性拉走 + 立刻删 key（防止重复读取）。
    """
    key = f"{RESULT_KEY_PREFIX}{task_id}"
    raw = await redis_client.blpop([key], timeout=timeout_s)
    if raw is None:
        logger.warning(f"pdd_app_queue: timeout waiting for task_id={task_id}")
        return None
    # raw 是 (key, value) 元组
    _, value = raw
    try:
        return PddAppResult.model_validate_json(value)
    except Exception as exc:
        logger.exception(f"pdd_app_queue: failed to parse result for {task_id}: {exc}")
        return PddAppResult(task_id=task_id, status="failed", error=f"parse_error: {exc}")


# --- 供 backend HTTP bridge 调用（serve worker 端）---

async def pop_task(timeout_s: int = 30) -> PddAppTask | None:
    """worker poll endpoint 用：BLPOP 任务队列，最多阻塞 timeout_s 秒。

    timeout_s 不宜过长（避免 HTTP 连接长时间挂着占 backend 资源）；
    worker 端会循环 poll。
    """
    raw = await redis_client.blpop([TASK_QUEUE_KEY], timeout=timeout_s)
    if raw is None:
        return None
    _, value = raw
    try:
        return PddAppTask.model_validate_json(value)
    except Exception as exc:
        logger.exception(f"pdd_app_queue: malformed task in queue: {exc}, raw={value[:200]}")
        return None


async def push_result(result: PddAppResult) -> None:
    """worker result endpoint 用：把结果推到对应 key，并设短 TTL。"""
    key = f"{RESULT_KEY_PREFIX}{result.task_id}"
    payload = result.model_dump_json()
    pipe = redis_client.pipeline()
    pipe.rpush(key, payload)
    pipe.expire(key, RESULT_TTL_SECONDS)
    await pipe.execute()
    logger.info(
        f"pdd_app_queue: pushed result task_id={result.task_id} "
        f"status={result.status} items={len(result.items)}"
    )


async def record_worker_heartbeat(device_serials: list[str]) -> None:
    """worker 定期上报"我活着，连了这些手机"。"""
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "devices": device_serials,
    }
    await redis_client.set(
        WORKER_HEARTBEAT_KEY, json.dumps(payload), ex=120  # 2min 没心跳就视为离线
    )


async def get_worker_status() -> dict[str, Any]:
    """供管理面 / 自检脚本查 worker 在不在。"""
    raw = await redis_client.get(WORKER_HEARTBEAT_KEY)
    if not raw:
        return {"online": False, "devices": []}
    data = json.loads(raw)
    return {"online": True, **data}
