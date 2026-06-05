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
# 多 worker 单机多手机：本进程绑定的设备 serial（device_manager 据此过滤）。
# 多开时每个进程必须设不同的 ADB_SERIAL + WORKER_NAME + BOUND_PDD_ACCOUNT。
ADB_SERIAL = os.environ.get("ADB_SERIAL", "").strip()

# 这台 worker 上当前 PDD APP 登录的账号 account_name。
# 用途：(1) audit/日志（结果里 account_name）；(2) 多号路由——poll/心跳带它，
# backend 只发"分配给本号品类"的自动跑批任务（roadmap §15，见 http_client）。
# 换号时同步改这个值；为空就用 "unknown"（视为未配号，退回默认队列）。
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
BURST_SIZE_MIN = int(os.environ.get("BURST_SIZE_MIN", "3"))
BURST_SIZE_MAX = int(os.environ.get("BURST_SIZE_MAX", "5"))
INTRA_BURST_GAP_SECONDS_MIN = float(os.environ.get("INTRA_BURST_GAP_SECONDS_MIN", "5"))
INTRA_BURST_GAP_SECONDS_MAX = float(os.environ.get("INTRA_BURST_GAP_SECONDS_MAX", "30"))
# 全局浏览节奏因子（与 pdd_app_client._HUMANIZE_PACE 同名同义）。
# 1.0 = 原节奏；0.7 = 快 30%。这里只缩放 burst 内"想下个词"的 intra-gap；
# 不缩放 inter-burst 静默（账号画像关键）和 daily quota。clamp [0.3, 1.0]。
HUMANIZE_PACE = max(0.3, min(1.0, float(os.environ.get("HUMANIZE_PACE", "1.0"))))
INTER_BURST_GAP_MINUTES_MIN = float(os.environ.get("INTER_BURST_GAP_MINUTES_MIN", "5"))
INTER_BURST_GAP_MINUTES_MAX = float(os.environ.get("INTER_BURST_GAP_MINUTES_MAX", "30"))
DAILY_SEARCH_QUOTA = int(os.environ.get("DAILY_SEARCH_QUOTA", "30"))
# priority >= 这个阈值的任务跳 inter-burst quiet（5-30 min 静默期）。
# 与 backend pdd_app_queue.EMERGENCY_PRIORITY_THRESHOLD 保持一致（默认 8）。
# 普通任务 fire 默认 priority=1，远低于阈值，不会误触旁路。
EMERGENCY_PRIORITY_THRESHOLD = int(os.environ.get("EMERGENCY_PRIORITY_THRESHOLD", "8"))
# burst 内最后一次搜索后，等多久没新任务就强制关 burst（按 home 退后台）。
# 每个 burst 开启时随机抽一个值（不是固定 60s），避免"搜完正好 60s 退桌面"
# 的统计指纹。真人"看完结果到放下手机"的时间从 30s 到几分钟不等。
BURST_IDLE_TIMEOUT_SECONDS_MIN = float(os.environ.get("BURST_IDLE_TIMEOUT_SECONDS_MIN", "45"))
BURST_IDLE_TIMEOUT_SECONDS_MAX = float(os.environ.get("BURST_IDLE_TIMEOUT_SECONDS_MAX", "180"))

_shutdown = asyncio.Event()


class QuotaExhausted(RuntimeError):
    """今日 search quota 用完抛这个，由 _handle_search 转成失败结果。"""


class BurstScheduler:
    """阵发式任务调度器：模拟真人"小爆发 + 长间隔"的搜索节奏。

    用法（_handle_search 调用）::
        await scheduler.enforce_pre_search()   # 可能 sleep 数秒到数十分钟
        is_first = scheduler.is_first_in_burst  # burst 内第一个任务？
        # ...do the actual work...
        burst_continues = scheduler.mark_search_done()
        if not burst_continues:
            await press_home_on_device(serial)  # 仅 burst 结束才退 PDD

    跨天处理：用 UTC date 做 day key；新一天首次调用自动重置计数器。
    """

    def __init__(self) -> None:
        self._searches_today = 0
        self._day_key = self._today_key()
        self._burst_remaining = 0
        self._last_search_at: float = 0.0
        self._last_burst_ended_at: float = 0.0
        # burst 内已完成的任务数。is_first_in_burst 用它判断 burst 内位置：
        # 0 = 这是 burst 的第 1 个任务（需要"开 APP → 浏览 → 搜"全流程）
        # >0 = burst 内后续任务（PDD 仍前台，跳冷启动 + 跳 warmup）
        self._tasks_done_in_current_burst = 0
        # 本 burst 的 idle 关闭阈值（每次开 burst 随机抽 45-180s，避免固定 60s
        # 的统计指纹）。0 = 还没开 burst / 已经关闭。
        self._current_burst_idle_timeout: float = 0.0
        # inter-burst 静默期"插一次拟人动作"的回调（如查物流 B 方案）。
        # async callable，无参；None = 不插。由 main 启动时注册。
        self._quiet_break_cb = None

    def set_quiet_break_handler(self, cb) -> None:
        """注册 inter-burst 静默期中段要插入的拟人动作回调（async, 无参, 自吞异常）。"""
        self._quiet_break_cb = cb

    async def _quiet_sleep_with_break(self, sleep_s: float) -> None:
        """睡满 inter-burst 静默期；中途随机挑一个点插入一次拟人动作（B 方案查物流）。

        真人在两波搜索之间的长间隔里，会偶尔点亮手机看一眼快递/订单——比"每次
        搜完立刻查一遍再放下"自然。这里把整段静默切成「睡一会 → 插动作 → 睡剩下」，
        插入动作耗时从剩余里扣掉，保证总静默 ≈ 目标 gap（不破坏 burst 节奏画像）。

        静默太短（< 90s）或没注册回调时，退化成普通整段 sleep。
        """
        cb = self._quiet_break_cb
        if cb is None or sleep_s < 90:
            await asyncio.sleep(sleep_s)
            return
        # 在 [25%, 75%] 区间随机挑一个点插入，别每次都在正中间（固定相位 = 指纹）
        break_at = sleep_s * random.uniform(0.25, 0.75)
        await asyncio.sleep(break_at)
        cb_started = time.monotonic()
        try:
            await cb()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"scheduler: quiet-break 回调异常(swallow): {exc}")
        cb_elapsed = time.monotonic() - cb_started
        remaining = sleep_s - break_at - cb_elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

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
            self._tasks_done_in_current_burst = 0
            self._current_burst_idle_timeout = 0.0
            self._last_burst_ended_at = 0.0

    @property
    def is_first_in_burst(self) -> bool:
        """True = burst 内的第 1 个任务（PDD 应做完整 cold-start + warmup 流程）。
        False = burst 内后续任务（PDD 仍前台，跳冷启动 + warmup，直接搜下一个词）。
        """
        return self._tasks_done_in_current_burst == 0

    def status(self) -> str:
        return (
            f"day={self._day_key} searches={self._searches_today}/{DAILY_SEARCH_QUOTA} "
            f"burst_remaining={self._burst_remaining}"
        )

    def snapshot(self) -> dict:
        """供心跳上报：当前 burst 状态的墙钟相对量，后端据此精确预估队列 ETA。

        ago 字段用 monotonic 算出"距今多少秒"，后端再按心跳延迟补偿。
        """
        self._maybe_reset_for_new_day()
        now = time.monotonic()
        in_quiet = self._burst_remaining <= 0 and self._last_burst_ended_at > 0
        return {
            "burst_remaining": max(0, self._burst_remaining),
            "in_quiet": in_quiet,
            # burst 内：距上次搜索结束多少秒（用于算下次 intra-gap 还剩多久）
            "last_search_ago_s": (now - self._last_search_at) if self._last_search_at > 0 else None,
            # quiet 期：距本波结束多少秒（用于算 inter-burst 静默还剩多久）
            "quiet_elapsed_s": (now - self._last_burst_ended_at) if in_quiet else None,
            "searches_today": self._searches_today,
            "quota": DAILY_SEARCH_QUOTA,
        }

    async def enforce_pre_search(self, priority: int = 1) -> None:
        """阻塞调用：等到下一次搜索可以执行。

        - 如果今天 quota 用完 → raise QuotaExhausted（紧急任务也不能突破，
          quota 是硬保护账号的底线）
        - 如果 priority >= EMERGENCY_PRIORITY_THRESHOLD（默认 8）且当前在
          inter-burst quiet 状态 → **跳过 quiet sleep，立即开新 burst**
          （日志会打 EMERGENCY-bypass 标记）
        - 否则按拟人化节奏走：burst 内 sleep intra-burst gap (5-30s)；
          burst 已结束 sleep inter-burst gap (5-30min) 后开新 burst

        ``priority`` 来自 PddAppTask.priority。普通任务默认 1，紧急任务由
        fire 脚本显式传 ≥ 8。backend enqueue 会把高优先级 LPUSH 到队首，
        所以 worker 拿到时这个旁路才有意义（否则被前面 FIFO 任务卡住）。
        """
        self._maybe_reset_for_new_day()

        if self._searches_today >= DAILY_SEARCH_QUOTA:
            raise QuotaExhausted(
                f"daily quota reached: {self._searches_today}/{DAILY_SEARCH_QUOTA} "
                f"(resets at UTC midnight)"
            )

        is_emergency = priority >= EMERGENCY_PRIORITY_THRESHOLD

        if self._burst_remaining <= 0:
            # 开新 burst 前要先等长 quiet 期（紧急任务跳过）
            if self._last_burst_ended_at > 0 and not is_emergency:
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
                    await self._quiet_sleep_with_break(sleep_s)
            elif self._last_burst_ended_at > 0 and is_emergency:
                # 紧急旁路：把 quiet 跳掉。reset _last_burst_ended_at 让后续
                # 普通任务的 quiet 计时从本紧急 burst 结束时算起，避免连续
                # 旁路把 quiet 完全废掉。
                elapsed = time.monotonic() - self._last_burst_ended_at
                logger.warning(
                    f"scheduler: EMERGENCY priority={priority} "
                    f"≥ {EMERGENCY_PRIORITY_THRESHOLD} — BYPASS inter-burst quiet "
                    f"(elapsed since last burst {elapsed / 60:.1f} min, "
                    f"opening new burst now)"
                )
                self._last_burst_ended_at = 0.0  # 清掉旧标记，新 burst 结束时会重设
            # 启动新 burst（剩余配额够时才打满，否则取剩余）
            remaining_quota = DAILY_SEARCH_QUOTA - self._searches_today
            desired = random.randint(BURST_SIZE_MIN, BURST_SIZE_MAX)
            self._burst_remaining = min(desired, remaining_quota)
            self._tasks_done_in_current_burst = 0
            # 抽本 burst 的 idle 关闭阈值（45-180s 随机）。每个 burst 不同，
            # 避免"最后一搜后 60s 退桌面"的固定指纹。
            self._current_burst_idle_timeout = random.uniform(
                BURST_IDLE_TIMEOUT_SECONDS_MIN, BURST_IDLE_TIMEOUT_SECONDS_MAX
            )
            logger.info(
                f"scheduler: new burst started — "
                f"{self._burst_remaining} searches planned, "
                f"idle_timeout={self._current_burst_idle_timeout:.0f}s "
                f"(daily so far {self._searches_today}/{DAILY_SEARCH_QUOTA})"
                f"{' [EMERGENCY]' if is_emergency else ''}"
            )
        else:
            # burst 内：随机短间隔（紧急任务也守这个 5-30s，避免连续 0 间隔异常）
            # 按 HUMANIZE_PACE 缩放（0.7 → 实际 3.5-21s），让整体节奏快 30%
            target_s = random.uniform(
                INTRA_BURST_GAP_SECONDS_MIN, INTRA_BURST_GAP_SECONDS_MAX
            ) * HUMANIZE_PACE
            elapsed = time.monotonic() - self._last_search_at
            if elapsed < target_s:
                sleep_s = target_s - elapsed
                logger.info(
                    f"scheduler: intra-burst gap — sleeping {sleep_s:.1f}s "
                    f"({self._burst_remaining} left in this burst)"
                    f"{' [emergency in-burst]' if is_emergency else ''}"
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
        self._tasks_done_in_current_burst += 1
        if self._burst_remaining <= 0:
            self._last_burst_ended_at = time.monotonic()
            self._tasks_done_in_current_burst = 0
            self._current_burst_idle_timeout = 0.0
            logger.info(
                f"scheduler: burst ended — daily total "
                f"{self._searches_today}/{DAILY_SEARCH_QUOTA}"
            )
            return False
        return True

    def maybe_end_idle_burst(self, idle_timeout_s: float | None = None) -> bool:
        """如果当前 burst 开着但已经太久没新任务，强制结束 burst。

        触发场景：scheduler 随机决定 burst_size=4，但 backend 只派了 2 个任务。
        没有这个超时，burst_remaining 永远停在 2，PDD 永远不退后台 = 拟人化
        破功。

        idle_timeout_s 的取值：
        - 默认 None：使用本 burst 开启时抽签的 self._current_burst_idle_timeout
          （45-180s 随机，每个 burst 不同）。**这是拟人化用法**，避免
          "搜完正好 60s 退桌面"成为统计指纹。
        - 传入数字：用 caller 给的值覆盖（兼容旧调用 / 测试场景）。

        :return: True = 强制结束了 burst（caller 应该把 PDD 退后台）；
                 False = burst 还没开 / 还没到超时 / 时间够近。
        """
        if self._burst_remaining <= 0:
            return False
        if self._last_search_at <= 0.0:
            return False
        threshold = idle_timeout_s if idle_timeout_s is not None else self._current_burst_idle_timeout
        if threshold <= 0:
            # 防御：burst 已经被 mark_search_done 关掉，timeout 被清成 0
            return False
        elapsed = time.monotonic() - self._last_search_at
        if elapsed < threshold:
            return False
        logger.info(
            f"scheduler: burst idle for {elapsed:.1f}s "
            f"(> {threshold:.0f}s sampled this burst) — force-ending burst "
            f"(was {self._burst_remaining} pending; daily total "
            f"{self._searches_today}/{DAILY_SEARCH_QUOTA})"
        )
        self._burst_remaining = 0
        self._tasks_done_in_current_burst = 0
        self._current_burst_idle_timeout = 0.0
        self._last_burst_ended_at = time.monotonic()
        return True


_scheduler = BurstScheduler()


async def _press_home_on_device(serial: str) -> None:
    """通过 adb 把当前 APP 切到后台。双策略：

    1. ``input keyevent KEYCODE_HOME``（最像真人按物理 home 键，但部分 Honor /
       华为 EMUI 机型对 PDD 这种全屏沉浸式 APP 响应不稳定）
    2. ``am start -a MAIN -c HOME``（显式启动 launcher 的 Intent，绝对兜底）

    2026-05-27 morning test 实测：Honor X20 + PDD 跑完任务后只发 KEYCODE_HOME
    日志显示成功，但 PDD 仍停在前台。补一次 am start launcher 才真正回到桌面。

    Burst 结束时调用——真人搜完一波不会一直停在 PDD 首页，会回桌面/切别的 APP。
    让 PDD 退后台几分钟，对反爬刻画"高频前台用户"的画像有降权效果。
    """
    async def _run_adb(*args: str, timeout: float = 5.0) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "adb", "-s", serial, "shell", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, err.decode(errors="ignore")[:200]

    try:
        rc1, err1 = await _run_adb("input", "keyevent", "KEYCODE_HOME")
        if rc1 != 0:
            logger.warning(f"[{serial}] KEYCODE_HOME rc={rc1} err={err1!r}")
        # 兜底：再发一次 launcher Intent，无论 keyevent 成功与否
        rc2, err2 = await _run_adb(
            "am", "start",
            "-a", "android.intent.action.MAIN",
            "-c", "android.intent.category.HOME",
        )
        if rc2 != 0:
            logger.warning(f"[{serial}] am start launcher rc={rc2} err={err2!r}")
        logger.info(
            f"[{serial}] PDD pushed to background "
            f"(KEYCODE_HOME rc={rc1}, launcher_intent rc={rc2})"
        )
    except Exception as exc:
        logger.warning(f"[{serial}] press_home failed: {exc}")


async def _quiet_logistics_break() -> None:
    """inter-burst 静默期中段插入的「查物流」动作（B 方案）。

    静默期 PDD 在后台、设备空闲，可安全独占一次：先廉价门控（开关/冷却/概率，
    不碰设备），命中才前台化 PDD 走查物流流程，结束退后台。best-effort，自吞异常。
    """
    from pdd_app_worker import pdd_app_client as _pac
    from pdd_app_worker.device_manager import healthy_serials
    from pdd_app_worker.pdd_app_client import PddAppClient

    # 用静默期(B)专属概率门控，与 burst 结尾(A)的概率独立
    if not _pac.should_browse_logistics(prob=_pac._LOGISTICS_QUIET_PROB):
        return
    devices = healthy_serials()
    if not devices:
        return
    serial = devices[0]
    logger.info(f"[{serial}] quiet-break: 静默期插入查物流")
    try:
        async with PddAppClient(serial) as cli:
            cli.set_cleanup_mode("exit")  # 查完按 home 退后台
            await cli.browse_logistics_now()
    finally:
        await _press_home_on_device(serial)


_scheduler.set_quiet_break_handler(_quiet_logistics_break)


def _setup_signals() -> None:
    """Windows 的 Ctrl+C 走 SIGINT。"""
    def _on_signal(*_: Any) -> None:
        logger.info("shutdown signal received")
        _shutdown.set()

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)


# backend 配置的 JSON key 大写后正好等于本模块的全局常量名
# （burst_size_min → BURST_SIZE_MIN ...），所以热更新可以用 globals() 直接赋值。
_REMOTE_INT_KEYS = {
    "burst_size_min", "burst_size_max",
    "daily_search_quota", "emergency_priority_threshold",
}
_REMOTE_FLOAT_KEYS = {
    "intra_burst_gap_seconds_min", "intra_burst_gap_seconds_max",
    "inter_burst_gap_minutes_min", "inter_burst_gap_minutes_max",
    "burst_idle_timeout_seconds_min", "burst_idle_timeout_seconds_max",
    "humanize_pace",
}


def apply_remote_config(cfg: dict[str, Any]) -> list[str]:
    """把 backend 拉到的配置热更新到本模块全局常量（用 global 赋值）。

    BurstScheduler 各方法运行时按模块全局名动态查找这些常量，所以更新后
    下一个 burst / 下一次 intra-gap 立即按新值走，无需重启 worker。
    humanize_pace 额外同步到 pdd_app_client（它各自维护一份）。

    :return: 真正变了的项的描述列表，供日志打印。
    """
    g = globals()
    changes: list[str] = []
    for key in _REMOTE_INT_KEYS | _REMOTE_FLOAT_KEYS:
        if key not in cfg:
            continue
        var = key.upper()
        try:
            val = int(cfg[key]) if key in _REMOTE_INT_KEYS else float(cfg[key])
        except (TypeError, ValueError):
            logger.warning(f"apply_remote_config: 跳过非法值 {key}={cfg[key]!r}")
            continue
        if g.get(var) != val:
            changes.append(f"{var} {g.get(var)}→{val}")
            g[var] = val
        if key == "humanize_pace":
            from pdd_app_worker import pdd_app_client
            pdd_app_client.set_humanize_pace(val)

    # 「查物流」拟人行为开关/概率：状态在 pdd_app_client 里维护，这里直接透传。
    # 两条独立概率：logistics_browse_prob(A=burst 结尾) / logistics_quiet_prob(B=静默期)。
    if any(k in cfg for k in ("logistics_browse_enabled", "logistics_browse_prob",
                              "logistics_quiet_prob")):
        from pdd_app_worker import pdd_app_client

        def _as_float(v):
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        enabled = bool(cfg.get("logistics_browse_enabled",
                               pdd_app_client._LOGISTICS_BROWSE_ENABLED))
        prob = _as_float(cfg.get("logistics_browse_prob"))
        quiet_prob = _as_float(cfg.get("logistics_quiet_prob"))
        old_en = pdd_app_client._LOGISTICS_BROWSE_ENABLED
        old_pr = pdd_app_client._LOGISTICS_BROWSE_PROB
        old_qpr = pdd_app_client._LOGISTICS_QUIET_PROB
        pdd_app_client.set_logistics_browse(enabled, prob, quiet_prob)
        if pdd_app_client._LOGISTICS_BROWSE_ENABLED != old_en:
            changes.append(f"LOGISTICS_BROWSE_ENABLED {old_en}→{pdd_app_client._LOGISTICS_BROWSE_ENABLED}")
        if prob is not None and pdd_app_client._LOGISTICS_BROWSE_PROB != old_pr:
            changes.append(f"LOGISTICS_BROWSE_PROB {old_pr}→{pdd_app_client._LOGISTICS_BROWSE_PROB}")
        if quiet_prob is not None and pdd_app_client._LOGISTICS_QUIET_PROB != old_qpr:
            changes.append(f"LOGISTICS_QUIET_PROB {old_qpr}→{pdd_app_client._LOGISTICS_QUIET_PROB}")
    return changes


async def _heartbeat_loop(client: "BackendClient") -> None:
    from pdd_app_worker.device_manager import healthy_serials

    last_devices: list[str] | None = None
    while not _shutdown.is_set():
        devices = healthy_serials()
        await client.send_heartbeat(devices, scheduler=_scheduler.snapshot())

        # 顺便拉运行时配置热更新调度参数（前端改过的会在这里生效，≤45s 延迟）
        cfg = await client.fetch_runtime_config()
        if cfg:
            changes = apply_remote_config(cfg)
            if changes:
                logger.info(f"runtime-config 热更新: {'; '.join(changes)}")

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
    priority = int(task.get("priority", 1))
    is_emergency = priority >= EMERGENCY_PRIORITY_THRESHOLD
    logger.info(
        f"received task {task_id} kind={kind} priority={priority}"
        f"{' [EMERGENCY]' if is_emergency else ''} payload={payload}"
    )

    try:
        if kind == "search":
            return await _handle_search(task_id, payload, started, priority=priority)
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


async def _handle_search(
    task_id: str,
    payload: dict[str, Any],
    started_at: float,
    priority: int = 1,
) -> dict[str, Any]:
    """kind=search：连第一台健康设备，跑 PddAppClient.search。

    阵发式调度：进入实际工作前调 _scheduler.enforce_pre_search()，可能
    阻塞数秒（burst 内）到数十分钟（burst 间）。每日 quota 用完会立即
    返回失败结果（不阻塞 worker，让 backend 看到明确的 quota_exhausted
    信号决定要不要 fallback 到别的采集通道）。
    """
    keyword = payload.get("keyword") or ""
    mode = payload.get("mode", "fast")
    # 派单方可指定 target_count / scroll_screens 覆盖默认值（fast=1 屏 / deep=3 屏）。
    # 任一为 None / 缺失就走 PddAppClient.search() 的默认行为。
    override_target_count = payload.get("target_count")
    override_scroll_screens = payload.get("scroll_screens")
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
        await _scheduler.enforce_pre_search(priority=priority)
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

    search_kwargs: dict[str, Any] = {"mode": mode}
    if isinstance(override_target_count, int) and override_target_count > 0:
        search_kwargs["max_items"] = override_target_count
    if isinstance(override_scroll_screens, int) and override_scroll_screens > 0:
        # search() 内部会 clamp 到 [1, 5]
        search_kwargs["scroll_screens"] = override_scroll_screens
    # burst 位置：first 走完整 cold-start + warmup；intra 跳过冷启动 + 强制 direct profile
    is_first = _scheduler.is_first_in_burst
    search_kwargs["is_first_in_burst"] = is_first

    # mark_search_done + cleanup_mode 决策必须在 __aexit__ 触发前完成：
    # - 如果 burst 还会继续 → cleanup 用 "soft"（不退 PDD，让下个任务接着搜）
    # - 如果 burst 结束 → cleanup 用 "exit"（按 home，PDD 退后台 5-30 min）
    burst_continues = False
    async with PddAppClient(serial) as cli:
        cli.set_cleanup_mode("soft")  # 临时；search 跑完后用真实结果覆盖
        search_result = await cli.search(keyword, **search_kwargs)
        burst_continues = _scheduler.mark_search_done()
        # 查物流触发点 A：burst 结束（burst_continues=False）且本次搜索没报错/
        # 没被风控时，在退 PDD 前按 burst-结尾概率(logistics_browse_prob)查一次。
        # 另有触发点 B（静默期中段，见 _quiet_logistics_break），两者共用每日探测/
        # 冷却状态、各自独立概率。best-effort 不影响主流程。
        if not burst_continues and not search_result.error:
            try:
                await cli.maybe_browse_logistics()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"maybe_browse_logistics swallowed: {exc}")
        cli.set_cleanup_mode("soft" if burst_continues else "exit")
    # __aexit__ 已用最新的 cleanup_mode 跑完 _post_task_cleanup

    # Belt + suspenders：如果 burst 结束，再走一次 _press_home_on_device。
    # 这条路径独立于 atx-agent（用 adb subprocess + am start launcher），是
    # Day 3.5 实测在 Honor X20 上唯一能真退 PDD 后台的兜底（见 §"踩坑"）。
    if not burst_continues:
        await _press_home_on_device(serial)

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
            # 队列空，继续 poll（http_client 已带长轮询）。但先检查一下：
            # 是不是上一波 burst 计划了 N 次搜索，实际只来了 K<N 次？这种情况下
            # _last_search_at 之后一直没新任务，要强制结束 burst + 退 PDD 后台。
            if _scheduler.maybe_end_idle_burst():
                try:
                    from pdd_app_worker.device_manager import healthy_serials as _hs
                    for s in _hs():
                        await _press_home_on_device(s)
                except Exception as exc:
                    logger.warning(f"idle-burst home press failed: {exc}")
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
        f"(BOUND_PDD_ACCOUNT={BOUND_PDD_ACCOUNT}, "
        f"ADB_SERIAL={ADB_SERIAL or '<all devices>'})"
    )
    # 多 worker 单机多手机时，没绑 ADB_SERIAL 又插着多台手机 → 所有进程会抢
    # 同一台（devices[0]），必须给每个进程设不同 ADB_SERIAL。这里强提示一下。
    if not ADB_SERIAL:
        try:
            from pdd_app_worker.device_manager import list_devices
            _connected = [d.serial for d in list_devices() if d.state == "device"]
            if len(_connected) > 1:
                logger.warning(
                    f"检测到 {len(_connected)} 台手机但未设 ADB_SERIAL："
                    f"{_connected}。多开 worker 时每个进程都会抢第一台 "
                    f"({_connected[0]})！请给每个进程设不同的 "
                    f"ADB_SERIAL + WORKER_NAME + BOUND_PDD_ACCOUNT。"
                )
        except Exception:
            pass
    logger.info(
        f"scheduler config: "
        f"burst_size=[{BURST_SIZE_MIN},{BURST_SIZE_MAX}] "
        f"intra_gap=[{INTRA_BURST_GAP_SECONDS_MIN:.0f},{INTRA_BURST_GAP_SECONDS_MAX:.0f}]s "
        f"inter_gap=[{INTER_BURST_GAP_MINUTES_MIN:.0f},{INTER_BURST_GAP_MINUTES_MAX:.0f}]min "
        f"burst_idle_timeout=[{BURST_IDLE_TIMEOUT_SECONDS_MIN:.0f},{BURST_IDLE_TIMEOUT_SECONDS_MAX:.0f}]s "
        f"daily_quota={DAILY_SEARCH_QUOTA} "
        f"emergency_priority≥{EMERGENCY_PRIORITY_THRESHOLD} "
        f"humanize_pace={HUMANIZE_PACE:.2f}"
        f"{' (FASTER)' if HUMANIZE_PACE < 1.0 else ''}"
    )
    if BOUND_PDD_ACCOUNT == "unknown":
        logger.warning(
            "BOUND_PDD_ACCOUNT 未设置 —— 任务结果里 account_name=unknown，"
            "出问题难定位是哪个号干的。建议在 .env 里加一行 "
            "`BOUND_PDD_ACCOUNT=pdd_crawler_xxxx`"
        )

    # 预热 OCR（百亿补贴价格兜底）。冷启动一次 ~2-5s，预热掉之后第一条
    # 真实任务不用等。失败不抛——OCR 是 best-effort，没它 worker 也能跑。
    # 模型必须事先用 `python -m pdd_app_worker.fetch_easyocr_models` 下到
    # ~/.EasyOCR/model/，否则这里也会失败但只是 warning。
    try:
        from pdd_app_worker import ocr as ocr_module
        t0 = time.monotonic()
        await asyncio.to_thread(ocr_module.preload_reader)
        logger.info(
            f"OCR preload done in {time.monotonic() - t0:.1f}s "
            f"(EasyOCR ch_sim+en, CPU)"
        )
    except Exception as exc:
        logger.warning(
            f"OCR preload skipped: {type(exc).__name__}: {exc}  "
            f"（百亿补贴价格识别会失效；普通卡片不受影响）"
        )

    _setup_signals()

    client = BackendClient()
    try:
        # 启动时先上报一次，建立 worker_status
        from pdd_app_worker.device_manager import ensure_adb_keyboard, healthy_serials
        devs = healthy_serials()
        # 把 ADB Keyboard 钉为当前输入法（重启自愈，防中文输入广播打空导致任务失败）
        ensure_adb_keyboard(devs)
        await client.send_heartbeat(devs, scheduler=_scheduler.snapshot())
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
