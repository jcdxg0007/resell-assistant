"""启动前烟测：依次验证 5 件事：

  1. 环境变量都填了
  2. adb 在 PATH 里
  3. 至少 1 台手机已连接（state=device）
  4. backend HTTPS endpoint 可达
  5. Bearer token 鉴权通过

任何一项失败立刻打印原因 + 怎么修。全部通过才能进 main.py。

跑法（在 worker venv 激活后）：
    python -m pdd_app_worker.smoke_test
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("smoke_test")


def check_env() -> bool:
    required = ["BACKEND_BASE_URL", "WORKER_TOKEN"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"❌ 缺少环境变量: {missing}")
        print("   修复：把 .env.example 复制为 .env 并填好值")
        return False
    if os.environ["WORKER_TOKEN"] == "change-me-pdd-worker-token":
        print("❌ WORKER_TOKEN 还是默认值，没改")
        return False
    print(f"✅ 环境变量 OK  backend={os.environ['BACKEND_BASE_URL']}")
    return True


def check_adb() -> bool:
    from pdd_app_worker.device_manager import list_devices

    devs = list_devices()
    if not devs:
        print("❌ 没检测到任何 USB 设备")
        print("   修复：")
        print("   1) 数据线插好（不是只能充电的线）")
        print("   2) 手机已开 USB 调试 + 一律允许")
        print("   3) Windows 装好厂商驱动（荣耀: HiSuite / OPPO: 手机助手）")
        return False
    print(f"✅ adb 检测到 {len(devs)} 台设备:")
    healthy = 0
    for d in devs:
        marker = "✅" if d.state == "device" else "⚠️"
        print(f"   {marker} {d.serial}  state={d.state}")
        if d.state == "device":
            healthy += 1
    if healthy == 0:
        print("❌ 没有 state=device 的健康设备")
        if any(d.state == "unauthorized" for d in devs):
            print("   修复：手机上没点'允许调试'，拔掉 USB 重插一次注意看手机弹窗")
        return False
    return True


async def check_backend() -> bool:
    from pdd_app_worker.http_client import BackendClient

    client = BackendClient()
    try:
        status = await client.get_status()
        print(f"✅ backend 可达 + token 鉴权通过  worker_status={status}")
        return True
    except Exception as exc:
        print(f"❌ backend 连接/鉴权失败: {type(exc).__name__}: {exc!r}")
        if "401" in str(exc):
            print("   修复：WORKER_TOKEN 跟 backend 不匹配。")
            print("   到 Sealos 控制台改 backend deploy 的 env PDD_WORKER_TOKEN，")
            print("   然后把同一个值填进 worker .env，重启两边。")
        elif "503" in str(exc):
            print("   修复：backend 还没设置 PDD_WORKER_TOKEN（用的是默认值）。")
            print("   到 Sealos 控制台设置 backend 的 env 变量 PDD_WORKER_TOKEN，")
            print("   值用一个随机长字符串，然后重启 backend pod。")
        elif "ConnectError" in str(exc) or "Name or service" in str(exc):
            print("   修复：BACKEND_BASE_URL 不对或网络问题。")
            print("   检查家里能不能 ping 通这个域名。")
        return False
    finally:
        await client.close()


async def main() -> int:
    print("=== PDD APP Worker 烟测 ===\n")
    ok_env = check_env()
    if not ok_env:
        return 1
    print()
    ok_adb = check_adb()
    if not ok_adb:
        return 2
    print()
    ok_backend = await check_backend()
    if not ok_backend:
        return 3
    print("\n🎉 全部通过！可以跑 `python -m pdd_app_worker.main` 启动 worker 了。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
