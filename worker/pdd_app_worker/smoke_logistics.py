"""「查物流」拟人行为单次冒烟（roadmap §11.4）。

强制在手机上走一遍：前台化 PDD → 个人中心 → 我的订单 →（有单则）查看物流
→ 停留滑动 → 返回。**绕过开关/概率/每日冷却**，纯粹让你肉眼看一遍流程、
顺便校准 selector。不占采集队列、不依赖 backend 部署。

跑法（在 worker venv 激活、手机已连 USB 后）：
    python -m pdd_app_worker.smoke_logistics

可选：SMOKE_SERIAL=xxxx 指定设备（多机时）；不指定就用第一台健康设备。

输出含三种结果：
    True  = 找到真实订单并查了物流（理想）
    False = 进到订单页但是空的（该号没真实订单 → 正式跑会当日冷却）
    None  = 没导航到订单页（个人中心/我的订单 控件没找到 → selector 待校准）
失败会打印走到哪一步，方便定位要改哪个 xpath。
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
logger = logging.getLogger("smoke_logistics")


async def main() -> int:
    from pdd_app_worker.device_manager import healthy_serials
    from pdd_app_worker.pdd_app_client import PddAppClient

    print("=== 查物流 拟人行为 单次冒烟 ===\n")

    serials = healthy_serials()
    if not serials:
        print("❌ 没有 state=device 的健康设备；先插好 USB / 允许调试，再跑 smoke_test。")
        return 2
    want = os.environ.get("SMOKE_SERIAL", "").strip()
    serial = want if (want and want in serials) else serials[0]
    print(f"✅ 用设备 {serial}（健康设备: {serials}）\n")

    async with PddAppClient(serial) as cli:
        # 收尾按 home 退后台（跟正式 burst 结尾一致）
        cli.set_cleanup_mode("exit")
        print("→ 前台化 PDD + 关弹窗 + 回首页 …")
        try:
            await cli._ensure_app_foreground()
            await cli._dismiss_popups()
            await cli._ensure_home_tab()
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 前置(前台化/回首页)出错（继续尝试查物流）: {exc!r}")

        print("→ 走查物流流程（个人中心 → 我的订单 → 查看物流）…\n")
        result = await cli._browse_logistics_once()

    print()
    if result is True:
        print("🎉 结果 = True：找到真实订单并查了物流。流程 OK，可以走 B（静默期插一次）。")
    elif result is False:
        print("⚠️ 结果 = False：进到订单页但为空。这个号没有真实订单——"
              "正式跑批会把它当日冷却。换个有真实订单的号再测，或先手动下一单。")
    else:
        print("❌ 结果 = None：没导航到订单页（个人中心 / 我的订单 控件没点到）。"
              "看上面日志走到哪一步，把对应页面 dump 出来校准 xpath：")
        print("   python -m uiautomator2 dump  (或 python -m uiautomator2.inspect)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
