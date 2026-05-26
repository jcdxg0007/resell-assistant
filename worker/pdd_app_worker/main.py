"""worker 主循环（Phase 1 Day 1 骨架）。

干三件事：
  1. 每 HEARTBEAT_INTERVAL_SECONDS 秒上报心跳 + 连接的手机列表
  2. 长轮询 backend 拉任务
  3. 收到任务后调 pdd_app_client 执行，结果推回 backend

Phase 1 Day 1 阶段：拉到任务直接返回 "not_implemented"，验证传输链路。
Day 2 起把 pdd_app_client 接进来真的开 PDD APP 操作。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pdd_app_worker")

HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "45"))
WORKER_NAME = os.environ.get("WORKER_NAME", "windows-home")

# Phase 1 单设备阶段：这台 worker 上当前 PDD APP 登录的账号 account_name。
# 仅作 audit/日志用途（worker 不切账号，PDD APP 实际登录的是谁就用谁）。
# 换号时同步改这个值；为空就用 "unknown"。
BOUND_PDD_ACCOUNT = os.environ.get("BOUND_PDD_ACCOUNT", "unknown")

_shutdown = asyncio.Event()


def _setup_signals() -> None:
    """Windows 的 Ctrl+C 走 SIGINT。"""
    def _on_signal(*_: Any) -> None:
        logger.info("shutdown signal received")
        _shutdown.set()

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)


async def _heartbeat_loop(client: "BackendClient") -> None:
    from pdd_app_worker.device_manager import healthy_serials

    last_devices: list[str] | None = None
    while not _shutdown.is_set():
        devices = healthy_serials()
        await client.send_heartbeat(devices)

        # 每次心跳都打印设备列表，方便运维肉眼盯线。状态变化时加 tag：
        #   [initial]      = worker 启动后首条
        #   [DISCONNECTED] = 上次有、本次空（WARNING 级别，cmd 里会变色）
        #   [RECONNECTED]  = 上次空、本次有
        #   [CHANGED]      = 列表变了但都非空（多设备场景）
        #   (无 tag)       = 跟上次一致
        if last_devices is None:
            logger.info(f"heartbeat: devices={devices} [initial]")
        elif not devices and last_devices:
            logger.warning(
                f"heartbeat: devices={devices} [DISCONNECTED, was {last_devices}]"
            )
        elif devices and not last_devices:
            logger.info(
                f"heartbeat: devices={devices} [RECONNECTED]"
            )
        elif devices != last_devices:
            logger.info(
                f"heartbeat: devices={devices} [CHANGED, was {last_devices}]"
            )
        else:
            logger.info(f"heartbeat: devices={devices}")
        last_devices = devices

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=HEARTBEAT_INTERVAL)
        except asyncio.TimeoutError:
            pass


async def _process_task(task: dict[str, Any]) -> dict[str, Any]:
    """根据 task.kind 分派到具体 handler。

    Phase 1 Day 2 起接 PddAppClient。kind=search 走真采集；其他 kind 暂时还
    没实现，返回 not_implemented 便于上游识别。
    """
    started = time.monotonic()
    task_id = task["task_id"]
    kind = task["kind"]
    payload = task.get("payload") or {}
    logger.info(f"received task {task_id} kind={kind} payload={payload}")

    try:
        if kind == "search":
            return await _handle_search(task_id, payload, started)
        if kind == "self_check":
            return await _handle_self_check(task_id, payload, started)
        # detail / history_price 等 Day 3+ 才实现
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "task_id": task_id,
            "status": "failed",
            "items": [],
            "risk_signals": [],
            "device_serial": None,
            "account_name": None,
            "elapsed_ms": elapsed_ms,
            "error": f"not_implemented_yet:kind={kind}",
            "raw_screenshot_path": None,
        }
    except Exception as exc:
        logger.exception(f"task {task_id} dispatcher crashed: {exc}")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "task_id": task_id,
            "status": "failed",
            "items": [],
            "risk_signals": [],
            "device_serial": None,
            "account_name": None,
            "elapsed_ms": elapsed_ms,
            "error": f"dispatcher_exception:{type(exc).__name__}:{exc}",
            "raw_screenshot_path": None,
        }


async def _handle_search(task_id: str, payload: dict[str, Any], started_at: float) -> dict[str, Any]:
    """kind=search：连第一台健康设备，跑 PddAppClient.search。

    Phase 1 Day 2 状态：链路通到 PddAppClient，但 _dump_visible_cards 还是
    占位（Day 3 用真机校准 XPath 后才能真正取到商品）。所以现在能验：
      - 开 PDD APP 不崩
      - 搜索栏能输入关键词
      - 没出风控墙
      - 但 items=[] 是预期的
    """
    keyword = payload.get("keyword") or ""
    mode = payload.get("mode", "fast")
    if not keyword:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        return {
            "task_id": task_id,
            "status": "failed",
            "items": [],
            "risk_signals": [],
            "device_serial": None,
            "account_name": None,
            "elapsed_ms": elapsed_ms,
            "error": "missing_keyword",
            "raw_screenshot_path": None,
        }

    from pdd_app_worker.device_manager import healthy_serials
    from pdd_app_worker.pdd_app_client import PddAppClient

    devices = healthy_serials()
    if not devices:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        return {
            "task_id": task_id,
            "status": "failed",
            "items": [],
            "risk_signals": ["no_device"],
            "device_serial": None,
            "account_name": None,
            "elapsed_ms": elapsed_ms,
            "error": "no_healthy_device",
            "raw_screenshot_path": None,
        }
    serial = devices[0]  # Phase 1 单机；Phase 2 按 account.bound_device_serial 路由

    async with PddAppClient(serial) as cli:
        search_result = await cli.search(keyword, mode=mode)

    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    if search_result.error:
        status = "risk_blocked" if any(
            s in ("slide_verify", "captcha", "login_wall",
                  "rate_limited", "real_name_wall")
            for s in search_result.risk_signals
        ) else "failed"
        return {
            "task_id": task_id,
            "status": status,
            "items": [],
            "risk_signals": search_result.risk_signals,
            "device_serial": serial,
            "account_name": BOUND_PDD_ACCOUNT,
            "elapsed_ms": elapsed_ms,
            "error": search_result.error,
            "raw_screenshot_path": search_result.raw_screenshot_path,
        }
    return {
        "task_id": task_id,
        "status": "ok",
        "items": search_result.items,
        "risk_signals": search_result.risk_signals,
        "device_serial": serial,
        "account_name": BOUND_PDD_ACCOUNT,
        "elapsed_ms": elapsed_ms,
        "error": None,
        "raw_screenshot_path": search_result.raw_screenshot_path,
    }


async def _handle_self_check(task_id: str, payload: dict[str, Any], started_at: float) -> dict[str, Any]:
    """kind=self_check：每天 backend 派一个空查询，看 worker + 手机 + PDD APP 是否健康。

    Day 4 接入；这里先返回简单的设备探测结果。
    """
    from pdd_app_worker.device_manager import list_devices, healthy_serials

    devices = list_devices()
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    return {
        "task_id": task_id,
        "status": "ok" if healthy_serials() else "failed",
        "items": [{"serial": d.serial, "state": d.state} for d in devices],
        "risk_signals": [] if healthy_serials() else ["no_device"],
        "device_serial": healthy_serials()[0] if healthy_serials() else None,
        "account_name": None,
        "elapsed_ms": elapsed_ms,
        "error": None if healthy_serials() else "no_healthy_device",
        "raw_screenshot_path": None,
    }


async def _poll_loop(client: "BackendClient") -> None:
    while not _shutdown.is_set():
        try:
            task = await client.poll_task()
        except Exception as exc:
            logger.exception(f"poll_task fatal: {exc}")
            await asyncio.sleep(5)
            continue
        if task is None:
            # 队列空，继续 poll（http_client 已带长轮询）
            continue
        try:
            result = await _process_task(task)
            await client.push_result(result)
        except Exception as exc:
            logger.exception(f"task processing failed: {exc}")
            # 尝试推一个失败结果回去，避免 caller 干等到 timeout
            await client.push_result({
                "task_id": task["task_id"],
                "status": "failed",
                "items": [],
                "risk_signals": [],
                "device_serial": None,
                "account_name": None,
                "elapsed_ms": None,
                "error": f"worker_exception: {type(exc).__name__}: {exc}",
                "raw_screenshot_path": None,
            })


async def main() -> int:
    # 延迟 import 避免 .env 读不到
    from pdd_app_worker.http_client import BackendClient

    logger.info(
        f"pdd_app_worker {WORKER_NAME} starting "
        f"(BOUND_PDD_ACCOUNT={BOUND_PDD_ACCOUNT})"
    )
    if BOUND_PDD_ACCOUNT == "unknown":
        logger.warning(
            "BOUND_PDD_ACCOUNT 未设置 —— 任务结果里 account_name=unknown，"
            "出问题难定位是哪个号干的。建议在 .env 里加一行 "
            "`BOUND_PDD_ACCOUNT=pdd_crawler_xxxx`"
        )
    _setup_signals()

    client = BackendClient()
    try:
        # 启动时先上报一次，建立 worker_status
        from pdd_app_worker.device_manager import healthy_serials
        devs = healthy_serials()
        await client.send_heartbeat(devs)
        logger.info(f"initial heartbeat sent: devices={devs}")

        # 并发跑心跳 + 任务 poll
        await asyncio.gather(
            _heartbeat_loop(client),
            _poll_loop(client),
        )
    finally:
        await client.close()
        logger.info("pdd_app_worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
