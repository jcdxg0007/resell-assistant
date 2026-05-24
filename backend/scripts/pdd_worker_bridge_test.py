"""验证 backend HTTP bridge for PDD APP worker 的连通性。

跑在 Sealos backend pod 里：
    kubectl -n ns-3zn44u6p exec deploy/backend -- python3 scripts/pdd_worker_bridge_test.py

检查内容：
  1. settings.PDD_WORKER_TOKEN 不是默认值
  2. Redis 队列 / 结果 key 都能正常 rpush + blpop
  3. enqueue_task → pop_task 链路对得上
  4. push_result → await_result 链路对得上
  5. heartbeat 写得进去

走的是直接的服务层调用（绕开 HTTPS），所以哪怕 backend pod 在被远程访问时挂了也能跑。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# 让脚本无论从哪儿启动都能 import app.*（pod 内 /app 是 backend 根）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services.pdd_app_queue import (  # noqa: E402
    PddAppResult,
    PddAppTask,
    await_result,
    enqueue_task,
    get_worker_status,
    pop_task,
    push_result,
    record_worker_heartbeat,
)


async def check_token() -> bool:
    s = get_settings()
    if s.PDD_WORKER_TOKEN in ("", "change-me-pdd-worker-token"):
        print("❌ PDD_WORKER_TOKEN 还是默认值，未配置。")
        print("   修复：到 Sealos 控制台给 backend deploy 加 env PDD_WORKER_TOKEN")
        print("   值用一个随机长字符串（建议 32+ 字符），重启 backend pod 后再跑此测试。")
        return False
    print(f"✅ PDD_WORKER_TOKEN 已配置（{len(s.PDD_WORKER_TOKEN)} chars）")
    return True


async def check_enqueue_pop() -> bool:
    t = PddAppTask(kind="search", payload={"keyword": "smoke-test"})
    await enqueue_task(t)
    popped = await pop_task(timeout_s=2)
    if popped is None:
        print("❌ enqueue → pop 链路断了：pop 超时拿不到任务")
        return False
    if popped.task_id != t.task_id:
        print(f"❌ pop 出来的 task_id 不对 {popped.task_id} vs {t.task_id}")
        return False
    print(f"✅ enqueue → pop OK  task_id={t.task_id[:8]}")
    return True


async def check_result_roundtrip() -> bool:
    """先 push_result，再 await_result，应该 1s 内拿到。"""
    t = PddAppTask(kind="search")
    r = PddAppResult(
        task_id=t.task_id, status="ok",
        items=[{"title": "smoke-test-item"}],
        device_serial="SMOKE_SERIAL",
        elapsed_ms=100,
    )
    started = time.monotonic()
    # 模拟 worker 那边推结果
    await push_result(r)
    # 模拟 caller 这边等结果
    got = await await_result(t.task_id, timeout_s=2)
    elapsed = time.monotonic() - started
    if got is None:
        print("❌ await_result 超时，结果链路断了")
        return False
    if got.task_id != t.task_id or got.status != "ok":
        print(f"❌ 结果不对  got={got}")
        return False
    print(f"✅ result 往返 OK  耗时 {elapsed*1000:.0f}ms")
    return True


async def check_heartbeat() -> bool:
    await record_worker_heartbeat(["SMOKE_DEV_A", "SMOKE_DEV_B"])
    status = await get_worker_status()
    if not status.get("online"):
        print(f"❌ heartbeat 写完后查 status 不是 online: {status}")
        return False
    devs = status.get("devices", [])
    if "SMOKE_DEV_A" not in devs:
        print(f"❌ heartbeat 里的设备列表丢了: {status}")
        return False
    print(f"✅ heartbeat 链路 OK  {status}")
    return True


async def main() -> int:
    print("=== PDD APP Worker Bridge 烟测 ===\n")
    if not await check_token():
        return 1
    if not await check_enqueue_pop():
        return 2
    if not await check_result_roundtrip():
        return 3
    if not await check_heartbeat():
        return 4
    print("\n🎉 backend HTTP bridge 全部链路通。")
    print("下一步：把 PDD_WORKER_TOKEN 的值告诉家里 worker 也填一样的，")
    print("       然后在 Windows 上跑 `python -m pdd_app_worker.smoke_test`。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
