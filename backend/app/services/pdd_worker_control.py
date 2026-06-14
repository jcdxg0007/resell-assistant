"""PDD worker 管家(supervisor)控制面：命令队列 + 状态快照（Redis 实现）。

家里 Windows PC 在 NAT 后面，后端无法主动连它，所以沿用「worker 主动拉」模式：
- 前端点按钮 → 后端把命令 rpush 进命令队列；
- 家里那个常驻的 **管家进程** 长轮询拉走命令、执行（检测设备 / 启停 worker 子
  进程 / git 更新），并把最新状态 POST 回来存进 Redis；
- 前端再读这份状态快照渲染面板。

命令与状态都走短 TTL 的 Redis key，和 worker 心跳同一套思路（见 pdd_app_queue）。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis_client
from app.models.system import Account

logger = logging.getLogger(__name__)

# Redis keys
CMD_QUEUE_KEY = "pdd_app:supervisor:cmd_q"      # 待执行命令（FIFO）
STATUS_KEY = "pdd_app:supervisor:status"        # 管家最新状态快照（短 TTL）

_CMD_TTL_SECONDS = 300       # 命令 5min 没被拉走就作废（避免管家离线时堆命令）
_STATUS_TTL_SECONDS = 90     # 90s 没上报状态视为管家离线

# 前端能下发的命令白名单。serial 维度的命令需带 serial；全局命令忽略 serial。
ALLOWED_ACTIONS = {
    "scan",        # 立刻重扫设备并上报（无副作用）
    "start",       # 启动某台手机的 worker（需 serial）
    "stop",        # 停某台手机的 worker（需 serial）
    "restart",     # 重启某台手机的 worker（需 serial）
    "start_all",   # 启动所有「已连接且已绑号」的手机
    "stop_all",    # 停所有 worker
    "update",      # git pull + 重启所有在跑的 worker
}
_SERIAL_REQUIRED = {"start", "stop", "restart"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def enqueue_supervisor_command(
    action: str, serial: str | None = None
) -> dict[str, Any]:
    """前端下发一条命令。返回入队的命令对象（含 id）。

    action 不在白名单 / 缺 serial 时抛 ValueError，由 API 层转 400。
    """
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"unknown action: {action}")
    if action in _SERIAL_REQUIRED and not serial:
        raise ValueError(f"action {action} requires serial")
    cmd = {
        "id": uuid.uuid4().hex,
        "action": action,
        "serial": serial or None,
        "ts": _now_iso(),
    }
    await redis_client.rpush(CMD_QUEUE_KEY, json.dumps(cmd))
    await redis_client.expire(CMD_QUEUE_KEY, _CMD_TTL_SECONDS)
    logger.info(f"supervisor cmd enqueued: {cmd}")
    return cmd


async def pop_supervisor_commands(max_n: int = 50) -> list[dict[str, Any]]:
    """管家轮询时把所有待执行命令一次性取走（FIFO）。"""
    out: list[dict[str, Any]] = []
    for _ in range(max_n):
        raw = await redis_client.lpop(CMD_QUEUE_KEY)
        if not raw:
            break
        try:
            out.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


async def set_supervisor_status(payload: dict[str, Any]) -> None:
    """管家上报最新状态快照（设备 / 子进程 / git commit / 最近命令结果）。"""
    data = dict(payload or {})
    data["ts"] = _now_iso()
    await redis_client.set(STATUS_KEY, json.dumps(data), ex=_STATUS_TTL_SECONDS)


async def get_supervisor_status() -> dict[str, Any]:
    """前端读管家状态。管家离线（key 过期）→ online=False。"""
    raw = await redis_client.get(STATUS_KEY)
    if not raw:
        return {"online": False}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"online": False}
    data["online"] = True
    return data


async def get_pdd_device_bindings(db: AsyncSession) -> dict[str, str]:
    """serial → account_name 映射（来自 accounts 表的 1机1号绑定）。

    管家据此在启动某台手机的 worker 时自动带上正确的 BOUND_PDD_ACCOUNT，
    免去本地一台台编辑 .bat。只取在用且已绑机的 PDD 采集号。
    """
    stmt = (
        select(Account.account_name, Account.bound_device_serial)
        .where(Account.platform == "pdd_crawler")
        .where(Account.is_active.is_(True))
        .where(Account.bound_device_serial.isnot(None))
    )
    rows = (await db.execute(stmt)).all()
    bindings: dict[str, str] = {}
    for account_name, serial in rows:
        if serial:
            bindings[serial] = account_name
    return bindings
