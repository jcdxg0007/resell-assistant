"""设备管理：发现 USB 连接的手机、维护设备状态。

Phase 1 Day 1 只实现 list_devices；Day 2 起加 connect_device、状态机等。
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import NamedTuple

logger = logging.getLogger(__name__)

# 多 worker 单机多手机场景：每个 worker 进程用 ADB_SERIAL 绑定自己那台手机。
# 一台电脑上 adb 能看到所有插着的手机，不绑定的话 N 个进程都会去抢 devices[0]
# （同一台）。设了 ADB_SERIAL，healthy_serials() 只返回这一台，互不打架。
# 不设（单 worker 旧行为）→ 返回全部健康设备，向后兼容。
_BOUND_SERIAL = os.environ.get("ADB_SERIAL", "").strip()


class Device(NamedTuple):
    serial: str
    state: str  # "device" | "offline" | "unauthorized"


def list_devices() -> list[Device]:
    """调 adb devices 返回当前连接的手机。

    Windows / Linux 均可，前提是 PATH 里有 adb.exe。
    """
    try:
        out = subprocess.run(
            ["adb", "devices"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except FileNotFoundError:
        logger.error("adb not found in PATH. 装 Android Platform Tools 并把它加到 PATH。")
        return []
    except subprocess.CalledProcessError as exc:
        logger.error(f"adb devices failed: {exc.stderr}")
        return []
    except subprocess.TimeoutExpired:
        logger.error("adb devices timed out")
        return []

    devices = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) >= 2:
            devices.append(Device(serial=parts[0], state=parts[1]))
    return devices


def healthy_serials() -> list[str]:
    """只返回 state == 'device' 的手机 serial。

    设了 ADB_SERIAL 就只返回绑定的那一台（多 worker 单机隔离）；否则返回全部。
    """
    serials = [d.serial for d in list_devices() if d.state == "device"]
    if _BOUND_SERIAL:
        return [s for s in serials if s == _BOUND_SERIAL]
    return serials


# uiautomator2 自带的输入法，worker 输入中文靠它。
ADB_KEYBOARD_IME = "com.github.uiautomator/.AdbKeyboard"


def ensure_adb_keyboard(serials: list[str] | None = None) -> None:
    """把 ADB Keyboard 设为各设备的当前输入法，让 worker 重启后自愈。

    背景：部分国产 ROM 默认输入法是搜狗等第三方，uiautomator2 首次切换 IME
    偶发不生效，导致 worker 打字时广播打空（AdbBroadcastError:
    ADB_KEYBOARD_CLEAR_TEXT failed: None），整条搜索任务失败。启动时显式
    enable + set 一次，把当前输入法钉死成 ADB Keyboard，后续 worker 在
    _type_keyword 里切换就稳了。失败只记日志、不阻断启动。
    """
    targets = serials if serials is not None else healthy_serials()
    for serial in targets:
        try:
            subprocess.run(
                ["adb", "-s", serial, "shell", "ime", "enable", ADB_KEYBOARD_IME],
                capture_output=True, text=True, timeout=10,
            )
            r = subprocess.run(
                ["adb", "-s", serial, "shell", "ime", "set", ADB_KEYBOARD_IME],
                capture_output=True, text=True, timeout=10,
            )
            msg = (r.stdout or r.stderr or "").strip()
            logger.info(f"[{serial}] ADB Keyboard 已设为当前输入法: {msg}")
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(f"[{serial}] 设置 ADB Keyboard 输入法失败（忽略，继续启动）: {exc}")
