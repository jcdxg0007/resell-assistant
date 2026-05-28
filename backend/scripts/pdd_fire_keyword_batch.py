"""批量派多个 PDD 搜索任务（一次性 enqueue，让 worker 在同一个 burst 内连刷）。

为什么需要这个：
- ``pdd_fire_one_task.py`` 是"派 1 个、await 结果"模式，适合手测单关键词
- 但 worker 端 BurstScheduler 的 burst 内 intra-gap 只有 5-30s，超过 60s
  没新任务进来 ``maybe_end_idle_burst`` 会强制结束 burst → 后续任务被
  推入 inter-burst quiet（5-30 min）
- 如果想"一波连续 3-5 个不同关键词"匹配真人模型，必须**先把这 3-5 个
  任务一次性 enqueue**，然后 worker 自然就在 1 个 burst 内做完

用法（在 Sealos backend pod 内）::

    python3 scripts/pdd_fire_keyword_batch.py 牙膏 洗手液 保温杯
    python3 scripts/pdd_fire_keyword_batch.py 牙膏 洗手液 保温杯 \\
            --mode deep --scroll-screens 5 --target-count 30

行为：
1. 立即把 N 个 search 任务 push 进 Redis 队列（按 keyword 顺序优先级递减）
2. 然后并发 await N 个 task 的结果，每个任务超时按 ``--per-task-timeout`` 算
3. 全部结果回来后打印聚合统计：每个 keyword 的 status + items + price_source 分布

退出码：
  0  所有任务 status="ok"，且每个 items ≥ 3
  1  有任务 items < 3 但都成功
  2  有任务 status="partial"
  3  有任务 status="failed"
  4  有任务 status="risk_blocked"  ← 立刻按 §5 应急处置（断 worker、隔离账号）
  5  有任务在 timeout 内没回来
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


_PRICE_SRC_TAG = {
    "xml":       "📄",
    "ocr":       "🔍",
    "ocr_error": "⚠ ",
    "missing":   "❌",
}


def _fmt_items(items: list[dict], max_show: int = 5) -> str:
    lines = []
    for i, it in enumerate(items[:max_show], 1):
        title = (it.get("title") or "")[:35]
        price = it.get("price") or "?"
        src = it.get("price_source", "?")
        tag = _PRICE_SRC_TAG.get(src, src)
        sales = it.get("sales", 0)
        ad = " [AD]" if it.get("is_ad") else ""
        sales_str = f"{sales}" if sales else "?"
        lines.append(
            f"      {i}. {tag} ¥{price:<6} 销{sales_str:<5}{ad} {title}"
        )
    if len(items) > max_show:
        lines.append(f"      ... 还有 {len(items) - max_show} 条")
    return "\n".join(lines) if lines else "      (空)"


def _fmt_aux_stats(items: list[dict]) -> str:
    """额外统计：广告占比、销量分布、bounds 覆盖率。"""
    if not items:
        return ""
    total = len(items)
    ad_n = sum(1 for it in items if it.get("is_ad"))
    sales_have = sum(1 for it in items if it.get("sales", 0) > 0)
    bounds_have = sum(1 for it in items if it.get("bounds"))
    return (
        f"      广告={ad_n}/{total}({ad_n * 100 / total:.0f}%) · "
        f"销量字段={sales_have}/{total}({sales_have * 100 / total:.0f}%) · "
        f"坐标={bounds_have}/{total}"
    )


def _fmt_price_source_stats(items: list[dict]) -> str:
    if not items:
        return "      (无 items)"
    counts: dict[str, int] = {}
    for it in items:
        counts[it.get("price_source", "unknown")] = counts.get(it.get("price_source", "unknown"), 0) + 1
    total = len(items)
    parts = []
    for src in ("xml", "ocr", "ocr_error", "missing", "unknown"):
        n = counts.get(src, 0)
        if n:
            parts.append(f"{src}={n}({n * 100 / total:.0f}%)")
    coverage = (counts.get("xml", 0) + counts.get("ocr", 0)) * 100 / total
    parts.append(f"覆盖率={coverage:.0f}%")
    return "      " + " · ".join(parts)


async def _build_task(
    keyword: str,
    mode: str,
    scroll_screens: int,
    target_count: int,
    timeout_s: int,
    priority: int,
) -> PddAppTask:
    return PddAppTask(
        kind="search",
        payload={
            "keyword": keyword,
            "target_count": target_count,
            "scroll_screens": scroll_screens,
            "mode": mode,
        },
        priority=priority,
        timeout_s=timeout_s,
    )


async def _await_task(task: PddAppTask, keyword: str, timeout_s: int) -> tuple[str, object]:
    """await 已 enqueue 任务的结果。"""
    result = await await_result(task.task_id, timeout_s=timeout_s)
    return keyword, result


async def main() -> int:
    parser = argparse.ArgumentParser(description="批量派多个 PDD 搜索任务")
    parser.add_argument("keywords", nargs="+", help="一个或多个关键词，例如 牙膏 洗手液 保温杯")
    parser.add_argument("--mode", default="deep", choices=("fast", "deep"))
    parser.add_argument("--scroll-screens", type=int, default=5)
    parser.add_argument("--target-count", type=int, default=30)
    parser.add_argument(
        "--per-task-timeout", type=int, default=300,
        help="单个任务超时秒数（默认 300=5min；任务在 burst 内 intra-gap 5-30s + 实际 30-60s 完成）"
    )
    parser.add_argument(
        "--emergency", action="store_true",
        help="紧急批次：所有任务用 priority=9 → LPUSH 插队首 + worker 跳 inter-burst quiet。"
             "建议只在等不及 5-30 min 静默期时用，且单次紧急批不超过 3 个关键词（拟人化考虑）。"
    )
    parser.add_argument(
        "--save-json", metavar="PATH", default=None,
        help="把完整结果（所有 keyword 的所有 items 含 raw 字段）落到 JSON 文件。"
             "建议每次跑批都开，方便积累数据质量样本与跨周对比。"
             "示例：--save-json /tmp/pdd_batch_$(date +%%Y%%m%%d_%%H%%M).json"
    )
    args = parser.parse_args()

    n = len(args.keywords)
    is_emergency = args.emergency
    priority = 9 if is_emergency else 1

    if n > 5:
        print(f"⚠ 一波派超过 5 个 = 超过 burst_size 上限，多出的会被甩到下一个 burst")
        print(f"  （worker BURST_SIZE_MAX 默认 5，跨 burst 之间会有 5-30 min 静默）")
        print()
    if is_emergency and n > 3:
        print(f"⚠ 紧急批 {n} 个关键词偏多，建议 ≤ 3。多了仍按 burst_size 上限切分。\n")

    print(f"\n=== PDD APP 批量派任务{' [EMERGENCY]' if is_emergency else ''} ===\n")

    status = await get_worker_status()
    if not status.get("online"):
        print("❌ worker 不在线（没收到心跳）。")
        return 5
    devs = status.get("devices", [])
    print(f"✅ worker 在线  devices={devs}  last_ts={status.get('ts')}\n")
    print(f"准备派 {n} 个任务（mode={args.mode}, scroll_screens={args.scroll_screens}, "
          f"target_count={args.target_count}, priority={priority}"
          f"{'[EMERGENCY/jump-queue + skip-quiet]' if is_emergency else ''}）：")
    for kw in args.keywords:
        print(f"  · {kw}")
    print()

    # ─ 步骤 1: enqueue。普通：按命令行顺序 RPUSH（FIFO，第一个先做）。
    #   紧急：反向 LPUSH，让命令行第一个词留在队首（因为后 LPUSH 的会
    #   被压到更前面）。enqueue 顺序串行化（不要 asyncio.gather），防止
    #   asyncio 调度乱序破坏队列里的关键词顺序。
    print("── enqueue all ──")
    built_tasks: list[tuple[str, PddAppTask]] = []
    for kw in args.keywords:
        t = await _build_task(
            kw, args.mode, args.scroll_screens, args.target_count,
            args.per_task_timeout, priority,
        )
        built_tasks.append((kw, t))

    enqueue_order = list(reversed(built_tasks)) if is_emergency else built_tasks
    for kw, task in enqueue_order:
        await enqueue_task(task)
        tag = " [EMERGENCY]" if is_emergency else ""
        print(f"  → enqueued '{kw}'  task_id={task.task_id[:8]}  priority={priority}{tag}")

    # ─ 步骤 2: 并发等所有结果回来
    print()
    started = time.monotonic()
    await_tasks = [
        asyncio.create_task(_await_task(task, kw, args.per_task_timeout))
        for kw, task in built_tasks
    ]
    results = await asyncio.gather(*await_tasks, return_exceptions=True)
    total_elapsed = time.monotonic() - started

    print(f"\n── 全部完成（总耗时 {total_elapsed:.1f}s）──\n")

    worst_status = "ok"
    n_items_low = 0
    overall_xml = 0
    overall_ocr = 0
    overall_total = 0

    for r in results:
        if isinstance(r, Exception):
            print(f"  ❌ exception: {type(r).__name__}: {r}")
            worst_status = max(worst_status, "failed", key=lambda s: ["ok", "low", "partial", "failed", "risk_blocked", "timeout"].index(s))
            continue
        kw, result = r
        if result is None:
            print(f"  ⏱ '{kw}'  ❌ 超时（{args.per_task_timeout}s 内未回结果，可能在 inter-burst quiet）")
            worst_status = max(worst_status, "timeout", key=lambda s: ["ok", "low", "partial", "failed", "risk_blocked", "timeout"].index(s))
            continue
        item_count = len(result.items)
        print(f"  · '{kw}'  status={result.status}  items={item_count}  "
              f"elapsed_ms={result.elapsed_ms}  risks={result.risk_signals}")
        if result.items:
            print(_fmt_items(result.items))
            print(_fmt_price_source_stats(result.items))
            print(_fmt_aux_stats(result.items))
            # 累计统计
            for it in result.items:
                overall_total += 1
                if it.get("price_source") == "xml":
                    overall_xml += 1
                elif it.get("price_source") == "ocr":
                    overall_ocr += 1
        if result.error:
            print(f"      error: {result.error}")
        # 升级 worst_status
        if result.status == "risk_blocked":
            worst_status = "risk_blocked"
        elif result.status == "failed" and worst_status not in ("risk_blocked",):
            worst_status = "failed"
        elif result.status == "partial" and worst_status not in ("risk_blocked", "failed"):
            worst_status = "partial"
        elif result.status == "ok" and item_count < 3 and worst_status == "ok":
            worst_status = "low"
            n_items_low += 1

    # ─ 步骤 3: 聚合分布
    print("\n── 聚合 price_source 分布（跨所有 keyword）──")
    if overall_total:
        cov = (overall_xml + overall_ocr) * 100 / overall_total
        print(f"  xml: {overall_xml} ({overall_xml * 100 / overall_total:.1f}%)  "
              f"ocr: {overall_ocr} ({overall_ocr * 100 / overall_total:.1f}%)  "
              f"覆盖率: {cov:.1f}%  "
              f"(total items: {overall_total})")
    else:
        print("  (无 items)")

    # ─ 步骤 4: 落 JSON（如指定）
    if args.save_json:
        import json
        from datetime import datetime, timezone
        # 解析 PddAppResult 对象到 dict，并把 keyword 关联进去
        dump_records = []
        for r in results:
            if isinstance(r, Exception):
                dump_records.append({"keyword": None, "exception": f"{type(r).__name__}: {r}"})
                continue
            kw, result = r
            if result is None:
                dump_records.append({"keyword": kw, "timeout": True})
                continue
            dump_records.append({
                "keyword": kw,
                "status": result.status,
                "items": result.items,
                "risk_signals": result.risk_signals,
                "elapsed_ms": result.elapsed_ms,
                "error": result.error,
                "device_serial": result.device_serial,
                "account_name": result.account_name,
            })
        payload = {
            "fired_at": datetime.now(timezone.utc).isoformat(),
            "mode": args.mode,
            "scroll_screens": args.scroll_screens,
            "target_count": args.target_count,
            "priority": priority,
            "emergency": is_emergency,
            "total_elapsed_s": round(total_elapsed, 1),
            "results": dump_records,
        }
        try:
            with open(args.save_json, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"\n✓ 完整结果已落盘：{args.save_json}")
        except OSError as exc:
            print(f"\n⚠ JSON 落盘失败：{exc}（结果仍会按返回码退出，但样本丢了）")

    print()
    if worst_status == "risk_blocked":
        print("🔴 红灯：有任务触发风控！立刻 Ctrl+C 停 worker，按 §5 应急处置。")
        return 4
    if worst_status == "failed":
        print("🔴 至少一个任务彻底失败。")
        return 3
    if worst_status == "timeout":
        print("🟡 有任务超时未回。可能 worker 在 inter-burst quiet（5-30 min）里。")
        return 5
    if worst_status == "partial":
        print("🟡 有任务部分成功。")
        return 2
    if worst_status == "low":
        print(f"🟡 全 ok，但 {n_items_low} 个任务 items < 3。冷门词或屏幕短。")
        return 1
    print("🟢 全部健康。按 §3 SOP 继续。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
