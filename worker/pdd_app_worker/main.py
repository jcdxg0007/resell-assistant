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
import random
import signal
import sys
import time
from datetime import datetime, timezone
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

# ── Burst-mode 任务调度配置 ─────────────────────────────────────────────
#
# 真人搜购物 APP 不是均匀的"每 30s 一次"，而是阵发性的：
#   - 想到一个需求 → 短时间内连搜 1-4 次（每次间 5-30s 思考）
#   - 然后离开几分钟到半小时
#   - 想到新需求又来一波
#
# 调度模型：BURST_SIZE 次搜索（intra_burst gap 短）+ INTER_BURST 静默期长。
# 每天总搜索数硬上限 DAILY_SEARCH_QUOTA。
BURST_SIZE_MIN = int(os.environ.get("BURST_SIZE_MIN", "1"))
BURST_SIZE_MAX = int(os.environ.get("BURST_SIZE_MAX", "4"))
INTRA_BURST_GAP_SECONDS_MIN = float(os.environ.get("INTRA_BURST_GAP_SECONDS_MIN", "5"))
INTRA_BURST_GAP_SECONDS_MAX = float(os.environ.get("INTRA_BURST_GAP_SECONDS_MAX", "30"))
INTER_BURST_GAP_MINUTES_MIN = float(os.environ.get("INTER_BURST_GAP_MINUTES_MIN", "5"))
INTER_BURST_GAP_MINUTES_MAX = float(os.environ.get("INTER_BURST_GAP_MINUTES_MAX", "30"))
DAILY_SEARCH_QUOTA = int(os.environ.get("DAILY_SEARCH_QUOTA", "30"))

_shutdown = asyncio.Event()


class QuotaExhausted(RuntimeError):
    """今日 search quota 用完抛这个，由 _handle_search 转成失败结果。"""


class BurstScheduler:
    """阵发式任务调度器：模拟真人"小爆发 + 长间隔"的搜索节奏。

    用法（_handle_search 调用）::
        await scheduler.enforce_pre_search()   # 可能 sleep 数秒到数十分钟
        # ...do the actual work...
        burst_continues = scheduler.mark_search_done()
        if not burst_continues:
            await press_home_on_device(serial)  # PDD 退后台

    跨天处理：用 UTC date 做 day key；新一天首次调用自动重置计数器。
    """

    def __init__(self) -> None:
        self._searches_today = 0
        self._day_key = self._today_key()
        self._burst_remaining = 0
        self._last_search_at: float = 0.0
        self._last_burst_ended_at: float = 0.0

    @staticmethod
    def _today_key() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _maybe_reset_for_new_day(self) -> None:
        today = self._today_key()
        if today != self._day_key:
            logger.info(
                f"scheduler: new day {today}, "
                f"yesterday total searches={self._searches_today}; resetting counters"
            )
            self._day_key = today
            self._searches_today = 0
            self._burst_remaining = 0
            self._last_burst_ended_at = 0.0

    def status(self) -> str:
        return (
            f"day={self._day_key} searches={self._searches_today}/{DAILY_SEARCH_QUOTA} "
            f"burst_remaining={self._burst_remaining}"
        )

    async def enforce_pre_search(self) -> None:
        """阻塞调用：等到下一次搜索可以执行。

        - 如果今天 quota 用完 → raise QuotaExhausted
        - 如果当前 burst 还没用完 → sleep intra-burst gap (5-30s)
        - 如果上一个 burst 已结束 → sleep inter-burst gap (5-30min) 后开新 burst
        """
        self._maybe_reset_for_new_day()

        if self._searches_today >= DAILY_SEARCH_QUOTA:
            raise QuotaExhausted(
                f"daily quota reached: {self._searches_today}/{DAILY_SEARCH_QUOTA} "
                f"(resets at UTC midnight)"
            )

        if self._burst_remaining <= 0:
            # 开新 burst 前要先等长 quiet 期
            if self._last_burst_ended_at > 0:
                target_s = random.uniform(
                    INTER_BURST_GAP_MINUTES_MIN * 60.0,
                    INTER_BURST_GAP_MINUTES_MAX * 60.0,
                )
                elapsed = time.monotonic() - self._last_burst_ended_at
                if elapsed < target_s:
                    sleep_s = target_s - elapsed
                    logger.info(
                        f"scheduler: inter-burst quiet — sleeping "
                        f"{sleep_s / 60:.1f} min before new burst "
                        f"(target gap {target_s / 60:.1f} min, "
                        f"elapsed since last burst {elapsed / 60:.1f} min)"
                    )
                    await asyncio.sleep(sleep_s)
            # 启动新 burst（剩余配额够时才打满，否则取剩余）
            remaining_quota = DAILY_SEARCH_QUOTA - self._searches_today
            desired = random.randint(BURST_SIZE_MIN, BURST_SIZE_MAX)
            self._burst_remaining = min(desired, remaining_quota)
            logger.info(
                f"scheduler: new burst started — "
                f"{self._burst_remaining} searches planned "
                f"(daily so far {self._searches_today}/{DAILY_SEARCH_QUOTA})"
            )
        else:
            # burst 内：随机短间隔
            target_s = random.uniform(
                INTRA_BURST_GAP_SECONDS_MIN, INTRA_BURST_GAP_SECONDS_MAX
            )
            elapsed = time.monotonic() - self._last_search_at
            if elapsed < target_s:
                sleep_s = target_s - elapsed
                logger.info(
                    f"scheduler: intra-burst gap — sleeping {sleep_s:.1f}s "
                    f"({self._burst_remaining} left in this burst)"
                )
                await asyncio.sleep(sleep_s)

    def mark_search_done(self) -> bool:
        """搜索完成后调用。

        :return: True = burst 还在继续（PDD 可以留前台等下一次）；
                 False = burst 刚结束（caller 应该把 PDD 退到后台）。
        """
        self._searches_today += 1
        self._last_search_at = time.monotonic()
        self._burst_remaining -= 1
        if self._burst_remaining <= 0:
            self._last_burst_ended_at = time.monotonic()
            logger.info(
                f"scheduler: burst ended — daily total "
                f"{self._searches_today}/{DAILY_SEARCH_QUOTA}"
            )
            return False
        return True


_scheduler = BurstScheduler()


async def _press_home_on_device(serial: str) -> None:
    """通过 adb 把当前 APP 切到后台（KEYCODE_HOME）。

    Burst 结束时调用——真人搜完一波东西不会一直停在 PDD 首页，会回桌面/
    切别的 APP。让 PDD 退后台几分钟，PDD 自己也会清掉一些短期会话状态，
    对反爬刻画"高频前台用户"的画像有降权效果。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "adb", "-s", serial, "shell", "input", "keyevent", "KEYCODE_HOME",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        logger.info(f"[{serial}] PDD pushed to background (KEYCODE_HOME)")
    except Exception as exc:
        logger.warning(f"[{serial}] press_home failed: {exc}")


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

    阵发式调度：进入实际工作前调 _scheduler.enforce_pre_search()，可能
    阻塞数秒（burst 内）到数十分钟（burst 间）。每日 quota 用完会立即
    返回失败结果（不阻塞 worker，让 backend 看到明确的 quota_exhausted
    信号决定要不要 fallback 到别的采集通道）。
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

    try:
        await _scheduler.enforce_pre_search()
    except QuotaExhausted as exc:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        logger.warning(f"task {task_id} denied: {exc}")
        return {
            "task_id": task_id,
            "status": "failed",
            "items": [],
            "risk_signals": ["daily_quota_exhausted"],
            "device_serial": None,
            "account_name": BOUND_PDD_ACCOUNT,
            "elapsed_ms": elapsed_ms,
            "error": f"daily_quota_exhausted: {exc}",
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
        burst_continues = _scheduler.mark_search_done()
        if not burst_continues:
            await _press_home_on_device(serial)
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
    burst_continues = _scheduler.mark_search_done()
    if not burst_continues:
        await _press_home_on_device(serial)
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
    logger.info(
        f"scheduler config: "
        f"burst_size=[{BURST_SIZE_MIN},{BURST_SIZE_MAX}] "
        f"intra_gap=[{INTRA_BURST_GAP_SECONDS_MIN:.0f},{INTRA_BURST_GAP_SECONDS_MAX:.0f}]s "
        f"inter_gap=[{INTER_BURST_GAP_MINUTES_MIN:.0f},{INTER_BURST_GAP_MINUTES_MAX:.0f}]min "
        f"daily_quota={DAILY_SEARCH_QUOTA}"
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
