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
from typing import Any

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pdd_app_worker")

HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "45"))
WORKER_NAME = os.environ.get("WORKER_NAME", "windows-home")

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

    while not _shutdown.is_set():
        devices = healthy_serials()
        await client.send_heartbeat(devices)
        logger.debug(f"heartbeat sent: devices={devices}")
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=HEARTBEAT_INTERVAL)
        except asyncio.TimeoutError:
            pass


async def _process_task(task: dict[str, Any]) -> dict[str, Any]:
    """Phase 1 Day 1：占位实现，返回 not_implemented。

    Day 2 接 PddAppClient 后会被替换。
    """
    started = time.monotonic()
    task_id = task["task_id"]
    kind = task["kind"]
    logger.info(f"[stub] received task {task_id} kind={kind}")
    # 模拟 1 秒"工作"
    await asyncio.sleep(1.0)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "task_id": task_id,
        "status": "failed",
        "items": [],
        "risk_signals": [],
        "device_serial": None,
        "account_name": None,
        "elapsed_ms": elapsed_ms,
        "error": "not_implemented_yet:phase1_day1_stub",
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

    logger.info(f"pdd_app_worker {WORKER_NAME} starting")
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
