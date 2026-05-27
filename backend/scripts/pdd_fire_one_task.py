"""手动派一条真任务给家里 PDD APP worker，等结果回来打印健康度报告。

跑在 Sealos backend pod 里：

    kubectl -n ns-3zn44u6p exec -it deploy/backend -- \
        python3 scripts/pdd_fire_one_task.py 纸巾

或在 backend pod 内：

    cd /app && python3 scripts/pdd_fire_one_task.py 纸巾 --timeout 180

用途：
- §3 morning test SOP 一个个手动派安全关键词
- 验证 worker 端拟人化改动是否生效
- 任一关键词触发风控时，立刻能在屏幕上看到 risk_signals 而不用翻日志

退出码：
  0  status="ok" 且至少 3 个 items
  1  status="ok" 但 items < 3（黄灯）
  2  status="partial"
  3  status="failed"
  4  status="risk_blocked"  ← 红灯！立刻停 worker，进 §5
  5  内部超时（worker 没在 timeout 内推回结果）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services.pdd_app_queue import (  # noqa: E402
    PddAppTask,
    await_result,
    enqueue_task,
    get_worker_status,
)


def _fmt_items(items: list[dict]) -> str:
    """打印每条商品的 title / price / sales 简表。"""
    lines = []
    for i, it in enumerate(items[:8], 1):
        title = (it.get("title") or "")[:40]
        price = it.get("price") or "?"
        sales = it.get("sales") or "?"
        lines.append(f"  {i}. ¥{price:<8} {sales:<10} {title}")
    if len(items) > 8:
        lines.append(f"  ... 还有 {len(items) - 8} 条")
    return "\n".join(lines) if lines else "  (空)"


async def main() -> int:
    parser = argparse.ArgumentParser(description="给 home worker 派一条 PDD 搜索任务")
    parser.add_argument("keyword", help="要搜的关键词，例如 '纸巾'")
    parser.add_argument(
        "--target-count", type=int, default=8,
        help="期望 worker 返回多少件商品（默认 8，warmup + 1-2 屏够了）"
    )
    parser.add_argument(
        "--scroll-screens", type=int, default=2,
        help="搜索结果页滚动多少屏（默认 2，更多会增加暴露时间）"
    )
    parser.add_argument(
        "--timeout", type=int, default=180,
        help="等结果的总超时秒数（默认 180，因为 worker 可能落在 burst 静默期需等待）"
    )
    parser.add_argument(
        "--priority", type=int, default=1,
        help="任务优先级（数字越大越优先，默认 1）"
    )
    args = parser.parse_args()

    print("\n=== PDD APP worker 单任务派发 ===\n")

    status = await get_worker_status()
    if not status.get("online"):
        print("❌ worker 不在线（没收到心跳）。")
        print("   先在家里 Windows 跑 `python -m pdd_app_worker.main`，等心跳到 Sealos 再来。")
        return 5
    devs = status.get("devices", [])
    print(f"✅ worker 在线  devices={devs}  last_ts={status.get('ts')}\n")

    task = PddAppTask(
        kind="search",
        payload={
            "keyword": args.keyword,
            "target_count": args.target_count,
            "scroll_screens": args.scroll_screens,
        },
        priority=args.priority,
        timeout_s=args.timeout,
    )
    print(f"→ enqueue task_id={task.task_id[:8]}  keyword='{args.keyword}'  "
          f"target_count={args.target_count}  scroll_screens={args.scroll_screens}")
    await enqueue_task(task)

    print(f"  等 worker 处理...（最多 {args.timeout}s；worker 静默期会自动让任务排队）")
    started = time.monotonic()
    result = await await_result(task.task_id, timeout_s=args.timeout)
    elapsed = time.monotonic() - started

    if result is None:
        print(f"\n❌ 等结果超时（{args.timeout}s 内 worker 没推回）。可能原因：")
        print("   - worker 正好处在 inter-burst 静默期里（5-30min），换关键词前先看 worker 日志")
        print("   - worker 进程崩了，去 Windows 终端看堆栈")
        print("   - 手机离线/锁屏（去本地看 adb devices）")
        return 5

    print(f"\n--- 结果 (实际耗时 {elapsed:.1f}s) ---")
    print(f"  status         : {result.status}")
    print(f"  device_serial  : {result.device_serial}")
    print(f"  account_name   : {result.account_name}")
    print(f"  elapsed_ms     : {result.elapsed_ms}")
    print(f"  items 数量     : {len(result.items)}")
    print(f"  risk_signals   : {result.risk_signals}")
    if result.error:
        print(f"  error          : {result.error}")

    if result.items:
        print("\n--- 抓到的商品 ---")
        print(_fmt_items(result.items))

    print()

    if result.status == "risk_blocked":
        print("🔴 红灯：worker 触发风控！立刻 Ctrl+C 停 worker，按 §5 应急处置。")
        if "real_name_wall" in result.risk_signals:
            print("   ⚠ real_name_wall（实名认证墙）—— 物理手机上千万别点'去认证'")
        return 4
    if result.status == "failed":
        print("🔴 任务彻底失败。查看 error 字段定位（可能是 worker 异常/网络）。")
        return 3
    if result.status == "partial":
        print("🟡 部分成功。items 数偏少但没触发风控，关键词可能冷门或 lazy-render 没救回。")
        return 2
    if result.status == "ok":
        if len(result.items) < 3:
            print("🟡 ok 但 items < 3，可能是冷门词或屏幕太短，建议换关键词再试一次。")
            return 1
        print("🟢 健康。继续按 §3 SOP 走下一个关键词（每两次任务之间留 5-30 分钟）。")
        return 0

    print(f"❓ 未知 status：{result.status}")
    return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
