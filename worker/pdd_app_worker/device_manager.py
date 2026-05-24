"""设备管理：发现 USB 连接的手机、维护设备状态。

Phase 1 Day 1 只实现 list_devices；Day 2 起加 connect_device、状态机等。
"""
from __future__ import annotations

import logging
import subprocess
from typing import NamedTuple

logger = logging.getLogger(__name__)


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
    """只返回 state == 'device' 的手机 serial。"""
    return [d.serial for d in list_devices() if d.state == "device"]
