"""按词库自动派 PDD 任务（从 selection_keywords 表里挑久未跑的词）。

这是从"手动列关键词调 pdd_fire_keyword_batch.py"切换到"词库轮播"
的主入口。逻辑：

  1. 从 selection_keywords 选 N 个满足以下条件的词：
       - pdd_safe = TRUE
       - is_active = TRUE
       - schedule_enabled = TRUE
       - 'pdd' ∈ target_platforms
       - (可选) category.slug = --category
     按 pdd_last_searched_at ASC NULLS FIRST 排序（最久没跑/从没跑过的优先）
  2. 一次性 enqueue（emergency 模式下 LPUSH 反序入队保持顺序）
  3. 并发 await 所有结果
  4. 写回每个词的 pdd_last_searched_at、pdd_last_status、pdd_searches_total++

用法（在 Sealos backend pod 内）::

    # 默认跑 3 个最久没跑过的词
    python -m scripts.pdd_fire_from_lib

    # 跑 5 个，且只在某个分类里挑
    python -m scripts.pdd_fire_from_lib --count 5 --category pdd-seeds

    # dry-run（只展示会选哪些词，不真派）
    python -m scripts.pdd_fire_from_lib --count 5 --dry-run

  ⚠ 每词 mode 来自 keyword.pdd_mode（fast / list_deep / detail_smart / detail_deep）
    fast/deep 已实装；其他模式 worker 会按 fast 处理直到 Phase 2 上线
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import select, text, func  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.models.selection import Category, Keyword  # noqa: E402
from app.services.pdd_app_queue import (  # noqa: E402
    PddAppTask,
    await_result,
    enqueue_task,
    get_worker_status,
)
from app.services.pdd_search_run import persist_search_run  # noqa: E402


def _price_stats(items: list[dict]) -> tuple[float | None, float | None]:
    """从 items 里抽 price_min / price_median，用于历史落库的趋势分析。"""
    prices = sorted(float(it["price"]) for it in items if it.get("price"))
    if not prices:
        return None, None
    return prices[0], prices[len(prices) // 2]


# pdd_mode → worker 端 mode 映射
# fast/deep 是 worker 现已实装的两档
# list_deep = deep（多滚屏，更多 items）
# detail_smart/detail_deep = Phase 2 才支持，目前 fallback 到 fast
_MODE_MAP = {
    "fast":          ("fast", 8,  2),
    "list_deep":     ("deep", 30, 5),
    "detail_smart":  ("fast", 8,  2),  # Phase 2 占位，暂按 fast
    "detail_deep":   ("fast", 8,  2),  # Phase 2 占位，暂按 fast
}


# JSON 列包含 'pdd'（target_platforms 是 JSON 不是 JSONB，
# 必须先 ::jsonb 才能用 @> 操作）。表名写死防 join 时歧义。
_PDD_PLATFORM_FILTER = text(
    "selection_keywords.target_platforms::jsonb @> '[\"pdd\"]'::jsonb"
)


async def _select_keywords(
    count: int, category_slug: str | None
) -> list[Keyword]:
    """挑 N 个词，遵循「burst 内同品类聚集 + burst 间品类轮换」。

    两步走：

    1. 锁定品类：在所有「有可调度 PDD 词」的品类里，挑整体最久没被碰过
       的那个 —— 按该品类下 ``MAX(pdd_last_searched_at) ASC NULLS FIRST``。
       全新品类（一个词都没跑过 → MAX=NULL）最优先；``random()`` 给同级
       品类打散。指定 ``--category`` 时跳过这步，直接锁定该品类。
    2. 品类内选词：从锁定品类里按 ``pdd_last_searched_at ASC NULLS FIRST``
       取 N 个（最久没跑优先；``random()`` 给完全同级的词打散顺序）。

    为什么这样：真人一次 session 的搜索主题是聚集的（要买婴儿用品就连搜
    婴儿床 / 围挡 / 地垫），不会「婴儿床 → 猫包 → 相机壳」大杂烩 —— 后者
    是比价采集器的典型指纹。靠"品类轮换"让长期覆盖均匀、又让每个 burst
    看起来像一个有真实需求的买家。详见 docs/PDD-自建采集-roadmap.md
    §"Day 4 词库选词策略"。

    边界：锁定品类里可跑词不足 N 个时，就只返回那几个（不跨品类硬凑，
    保持 session 主题纯净）。
    """
    async with AsyncSessionLocal() as db:
        # ── 步骤 1：锁定品类 ──────────────────────────────────
        if category_slug:
            cat = (
                await db.execute(
                    select(Category).where(Category.slug == category_slug)
                )
            ).scalar_one_or_none()
            if cat is None:
                return []
            chosen_cat_id = cat.id
        else:
            cat_stmt = (
                select(Category.id)
                .join(Keyword, Keyword.category_id == Category.id)
                .where(Keyword.pdd_safe.is_(True))
                .where(Keyword.is_active.is_(True))
                .where(Keyword.schedule_enabled.is_(True))
                .where(_PDD_PLATFORM_FILTER)
                .group_by(Category.id)
                .order_by(
                    func.max(Keyword.pdd_last_searched_at).asc().nullsfirst(),
                    func.random(),
                )
                .limit(1)
            )
            chosen_cat_id = (await db.execute(cat_stmt)).scalar_one_or_none()
            if chosen_cat_id is None:
                return []

        # ── 步骤 2：品类内选 N 个最久没跑的词 ─────────────────
        kw_stmt = (
            select(Keyword)
            .options(selectinload(Keyword.category))
            .where(Keyword.category_id == chosen_cat_id)
            .where(Keyword.pdd_safe.is_(True))
            .where(Keyword.is_active.is_(True))
            .where(Keyword.schedule_enabled.is_(True))
            .where(_PDD_PLATFORM_FILTER)
            .order_by(
                Keyword.pdd_last_searched_at.asc().nullsfirst(),
                Keyword.pdd_searches_total.asc(),
                func.random(),
            )
            .limit(count)
        )
        rows = (await db.execute(kw_stmt)).scalars().all()
        return list(rows)


async def _write_back_result(
    keyword_id: str, status: str, when: datetime
) -> None:
    """跑完一个任务后把状态写回 keyword 行。"""
    async with AsyncSessionLocal() as db:
        kw = await db.get(Keyword, keyword_id)
        if not kw:
            return
        kw.pdd_last_searched_at = when
        kw.pdd_last_status = status
        kw.pdd_searches_total = (kw.pdd_searches_total or 0) + 1
        await db.commit()


def _fmt_items_short(items: list[dict], max_show: int = 3) -> str:
    if not items:
        return "        (空)"
    lines = []
    for i, it in enumerate(items[:max_show], 1):
        title = (it.get("title") or "")[:28]
        price = it.get("price") or "?"
        sales = it.get("sales") or 0
        badges = it.get("badges") or []
        bstr = f" [{','.join(badges[:2])}]" if badges else ""
        lines.append(f"        {i}. ¥{price:<6} 销{sales:<6}{bstr} {title}")
    if len(items) > max_show:
        lines.append(f"        ... 还有 {len(items) - max_show} 条")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 PDD 词库挑久未跑的词自动派任务"
    )
    parser.add_argument(
        "--count", type=int, default=3,
        help="本次派几个词（默认 3 = 一个 burst 内做完）。"
             "受 worker BURST_SIZE_MAX=5 约束，>5 多出的会被甩到下一个 burst"
    )
    parser.add_argument(
        "--category", default=None,
        help="只在某个分类里挑词（slug，例 pdd-seeds）"
    )
    parser.add_argument(
        "--per-task-timeout", type=int, default=300,
        help="单任务超时秒数（默认 300）"
    )
    parser.add_argument(
        "--emergency", action="store_true",
        help="紧急模式：priority=9，LPUSH 插队 + 跳 inter-burst quiet"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只展示会选哪些词，不真派"
    )
    args = parser.parse_args()

    keywords = await _select_keywords(args.count, args.category)
    if not keywords:
        print("❌ 词库里没有满足条件的词（pdd_safe && is_active && "
              "schedule_enabled && 'pdd' ∈ target_platforms）")
        print("   先用 pdd_seed_keywords.py 入种")
        return 1

    print(f"\n=== PDD 词库自动派任务 "
          f"{'[EMERGENCY]' if args.emergency else ''} ===\n")
    cat_label = keywords[0].category.name if keywords[0].category else "?"
    print(f"本次锁定品类：【{cat_label}】 —— burst 内同品类聚集（拟人化）")
    print(f"挑出 {len(keywords)} 个词（品类内按 pdd_last_searched_at ASC NULLS FIRST）：")
    for k in keywords:
        last = (
            k.pdd_last_searched_at.strftime("%m-%d %H:%M")
            if k.pdd_last_searched_at else "—— 从未跑过 ——"
        )
        print(f"  · {k.text:<24}  mode={k.pdd_mode:<14} "
              f"runs={k.pdd_searches_total:>3}  last={last}")
    print()

    if args.dry_run:
        print("(dry-run，不实际派任务)")
        return 0

    status = await get_worker_status()
    if not status.get("online"):
        print("❌ worker 不在线（没收到心跳）")
        return 5
    print(f"✅ worker 在线  devices={status.get('devices')}")
    print()

    priority = 9 if args.emergency else 1

    # ─ 构造 + enqueue ───────────────────────────────────────────
    built: list[tuple[Keyword, PddAppTask]] = []
    for k in keywords:
        worker_mode, target_count, scroll_screens = _MODE_MAP.get(
            k.pdd_mode, _MODE_MAP["fast"]
        )
        task = PddAppTask(
            kind="search",
            payload={
                "keyword": k.text,
                "target_count": target_count,
                "scroll_screens": scroll_screens,
                "mode": worker_mode,
            },
            priority=priority,
            timeout_s=args.per_task_timeout,
        )
        built.append((k, task))

    enqueue_order = list(reversed(built)) if args.emergency else built
    print("── enqueue ──")
    for k, task in enqueue_order:
        await enqueue_task(task)
        print(f"  → '{k.text}'  task_id={task.task_id[:8]} "
              f"priority={priority}")
    print()

    # ─ 并发 await ───────────────────────────────────────────────
    started = time.monotonic()

    async def _wait_one(k: Keyword, t: PddAppTask):
        r = await await_result(t.task_id, timeout_s=args.per_task_timeout)
        return k, r

    waits = [asyncio.create_task(_wait_one(k, t)) for k, t in built]
    results = await asyncio.gather(*waits, return_exceptions=True)
    total_elapsed = time.monotonic() - started

    print(f"── 全部完成（{total_elapsed:.1f}s）──\n")

    # ─ 写回每条 keyword 的状态 + 落库历史 + 打印 ───────────────
    run_source = "emergency" if args.emergency else "lib"
    worst = "ok"
    for r in results:
        if isinstance(r, Exception):
            print(f"  ❌ exception: {type(r).__name__}: {r}")
            worst = "failed"
            continue
        k, result = r
        now = datetime.now(timezone.utc)
        cat_name = k.category.name if k.category else None
        worker_mode = _MODE_MAP.get(k.pdd_mode, _MODE_MAP["fast"])[0]
        if result is None:
            print(f"  ⏱ '{k.text}'  ❌ 超时（{args.per_task_timeout}s）")
            await _write_back_result(k.id, "timeout", now)
            await persist_search_run(
                status="timeout", keyword_text=k.text, keyword_id=str(k.id),
                source=run_source, category_name=cat_name, mode=worker_mode,
                priority=priority,
            )
            if worst == "ok":
                worst = "timeout"
            continue
        n_items = len(result.items)
        # 把"ok 但 items=0"也归类为 empty，帮调度过滤掉冷门词
        bucket = result.status
        if bucket == "ok" and n_items == 0:
            bucket = "empty"
        await _write_back_result(k.id, bucket, now)
        p_min, p_median = _price_stats(result.items)
        await persist_search_run(
            status=bucket, keyword_text=k.text, keyword_id=str(k.id),
            task_id=result.task_id, source=run_source, category_name=cat_name,
            mode=worker_mode, items_count=n_items,
            price_min=p_min, price_median=p_median,
            risk_signals=result.risk_signals, device_serial=result.device_serial,
            account_name=result.account_name, elapsed_ms=result.elapsed_ms,
            priority=priority, error=result.error,
        )
        print(f"  · '{k.text}'  status={result.status}  items={n_items}  "
              f"elapsed_ms={result.elapsed_ms}  risks={result.risk_signals}")
        if result.items:
            print(_fmt_items_short(result.items))
        if result.error:
            print(f"        error: {result.error}")
        # 升级 worst
        if result.status == "risk_blocked":
            worst = "risk_blocked"
        elif result.status == "failed" and worst not in ("risk_blocked",):
            worst = "failed"
        elif result.status == "partial" and worst not in ("risk_blocked", "failed"):
            worst = "partial"

    print()
    if worst == "risk_blocked":
        print("🔴 红灯：有任务触发风控！立刻停 worker，按 §5 应急处置。")
        return 4
    if worst == "failed":
        return 3
    if worst == "timeout":
        print("🟡 有任务超时（可能 worker 在 inter-burst quiet）")
        return 5
    if worst == "partial":
        return 2
    print("🟢 全部健康。词库状态已写回 DB。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
