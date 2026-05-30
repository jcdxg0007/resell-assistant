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
WORKER_HEARTBEAT_KEY = "pdd_app:worker:heartbeat"  # 旧版单 worker 心跳（向后兼容读）
# 多 worker：每个 worker 一个 key（按 worker_name）。get_worker_status 聚合所有。
WORKER_HEARTBEAT_PREFIX = "pdd_app:worker:heartbeat:"
COLLECTION_PAUSED_KEY = "pdd_app:collection_paused"  # 批量采集暂停标志（"1"/未设）

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


def scroll_screens_for(target_count: int) -> int:
    """按目标商品数推导 PDD worker 应滚的屏数。

    worker 的 fast 模式默认只滚 1 屏（写死），导致 target_count(单词商品量)
    形同虚设——第一屏几个就几个、根本不往下滚。这里按 ~5 件/屏估算需要的屏数，
    夹在 [2, 5]：至少给第二屏一次机会，又不超过 5 屏（PDD 风控按单 session
    滚动深度打分，建议 ≤5）。worker 的采集循环本身"够数即停"，所以传的是上限，
    够了不会硬滚满；某词结果稀疏时最多滚到 5 屏兜底。
    """
    import math
    n = math.ceil(max(1, int(target_count)) / 5)
    return max(2, min(n, 5))


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


async def record_worker_heartbeat(
    device_serials: list[str],
    scheduler: dict[str, Any] | None = None,
    worker_name: str | None = None,
) -> None:
    """worker 定期上报"我活着，连了这些手机"。

    scheduler 是 BurstScheduler 快照（burst_remaining / in_quiet / *_ago_s 等），
    用于 batch_start 精确预估 PDD 队列 ETA；可为空（旧 worker 不上报）。

    worker_name：多 worker 场景每个进程唯一名。写到 per-worker key，
    get_worker_status 聚合所有 worker。旧 worker 不传 → 退回旧的单 key，
    行为与改造前一致。
    """
    name = (worker_name or "").strip()
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "devices": device_serials,
        "name": name or "default",
    }
    if scheduler is not None:
        payload["scheduler"] = scheduler
    key = f"{WORKER_HEARTBEAT_PREFIX}{name}" if name else WORKER_HEARTBEAT_KEY
    await redis_client.set(key, json.dumps(payload), ex=120)  # 2min 没心跳视为离线


async def queue_depth() -> int:
    """当前排队中（worker 还没 BLPOP 走）的任务数。"""
    return int(await redis_client.llen(TASK_QUEUE_KEY) or 0)


async def purge_queue() -> int:
    """清空任务队列里还没被 worker 拉走的任务。返回清掉的条数。

    已被 worker BLPOP 走、正在跑的任务不受影响（"在跑的不打断"）。
    """
    n = await queue_depth()
    await redis_client.delete(TASK_QUEUE_KEY)
    logger.info(f"pdd_app_queue: purged {n} queued task(s)")
    return n


async def set_collection_paused(paused: bool) -> None:
    """设置/清除批量采集暂停标志。暂停后 fire_from_lib 轮播会跳过。"""
    if paused:
        await redis_client.set(COLLECTION_PAUSED_KEY, "1")
    else:
        await redis_client.delete(COLLECTION_PAUSED_KEY)


async def is_collection_paused() -> bool:
    return bool(await redis_client.get(COLLECTION_PAUSED_KEY))


# 批量任务的「预计开始时刻」计划：{keyword_text: {"pdd": epoch_ts, "xianyu": epoch_ts|None}}
# 开始任务时写入，console 据此算每个待采集词的预估开始倒计时。6h 自动过期。
BATCH_PLAN_KEY = "pdd_app:batch_plan"
_BATCH_PLAN_TTL = 6 * 3600


async def set_batch_plan(plan: dict[str, dict]) -> None:
    if plan:
        await redis_client.set(BATCH_PLAN_KEY, json.dumps(plan), ex=_BATCH_PLAN_TTL)
    else:
        await redis_client.delete(BATCH_PLAN_KEY)


async def get_batch_plan() -> dict[str, dict]:
    raw = await redis_client.get(BATCH_PLAN_KEY)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


async def clear_batch_plan() -> None:
    await redis_client.delete(BATCH_PLAN_KEY)


# 全自动跑批：下一次派词的预计时刻（epoch 秒）。beat tick 据此随机错峰，
# 避免每天固定钟点上线。1 天过期（跨天自然失效，新一天重新排）。
AUTO_NEXT_TS_KEY = "pdd_app:auto_next_ts"


async def set_auto_next_ts(ts: float) -> None:
    await redis_client.set(AUTO_NEXT_TS_KEY, str(ts), ex=24 * 3600)


async def get_auto_next_ts() -> float | None:
    raw = await redis_client.get(AUTO_NEXT_TS_KEY)
    if not raw:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


async def clear_auto_next_ts() -> None:
    await redis_client.delete(AUTO_NEXT_TS_KEY)


# 闲鱼全自动采集：与 PDD 独立的下一次派词时刻（闲鱼不走 worker 队列，
# 直接 celery instant_search，所以单独一个 key）。
XIANYU_AUTO_NEXT_TS_KEY = "xianyu:auto_next_ts"


async def set_xianyu_auto_next_ts(ts: float) -> None:
    await redis_client.set(XIANYU_AUTO_NEXT_TS_KEY, str(ts), ex=24 * 3600)


async def get_xianyu_auto_next_ts() -> float | None:
    raw = await redis_client.get(XIANYU_AUTO_NEXT_TS_KEY)
    if not raw:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


async def clear_xianyu_auto_next_ts() -> None:
    await redis_client.delete(XIANYU_AUTO_NEXT_TS_KEY)


async def get_worker_status() -> dict[str, Any]:
    """供管理面 / 自检脚本查 worker 在不在。聚合所有在线 worker。

    返回形状（向后兼容单 worker 的字段）：
    - online：任一 worker 活着即 True
    - devices：所有 worker 设备的并集（保序去重）
    - ts / scheduler：代表性 worker 的快照（给 batch_start ETA 用，挑最就绪那个）
    - workers：[{name, devices, ts, scheduler}, ...]
    - worker_count / device_count：数量统计
    """
    raws: list[str] = []
    # 新 per-worker keys
    try:
        async for key in redis_client.scan_iter(match=f"{WORKER_HEARTBEAT_PREFIX}*"):
            raw = await redis_client.get(key)
            if raw:
                raws.append(raw)
    except Exception as exc:  # noqa: BLE001 — scan 失败退回只读旧 key
        logger.warning(f"get_worker_status scan failed: {exc}")
    # 旧版单 key（未升级的 worker 仍写这里）
    legacy = await redis_client.get(WORKER_HEARTBEAT_KEY)
    if legacy:
        raws.append(legacy)

    workers: list[dict[str, Any]] = []
    for raw in raws:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        workers.append({
            "name": data.get("name") or "default",
            "devices": data.get("devices") or [],
            "ts": data.get("ts"),
            "scheduler": data.get("scheduler"),
        })

    if not workers:
        return {
            "online": False, "devices": [],
            "workers": [], "worker_count": 0, "device_count": 0,
        }

    # 设备并集（保序去重）
    devices: list[str] = []
    for w in workers:
        for d in w["devices"]:
            if d not in devices:
                devices.append(d)

    # 代表性 scheduler：挑"最就绪"的 worker（不在静默期、burst 剩得多 → ETA 最小）。
    # 单 worker 时就是它本身，ETA 行为与改造前完全一致。
    def _readiness(w: dict[str, Any]) -> tuple[bool, int]:
        snap = w.get("scheduler") or {}
        return (bool(snap.get("in_quiet")), -int(snap.get("burst_remaining") or 0))

    rep = min(workers, key=_readiness)

    return {
        "online": True,
        "devices": devices,
        "ts": rep.get("ts"),
        "scheduler": rep.get("scheduler"),
        "workers": workers,
        "worker_count": len(workers),
        "device_count": len(devices),
    }
