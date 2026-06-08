"""深度收割链路只读体检：一条命令看「收割 → 落库」三段状态。

部署批 2 后，跑一个 deep 词，再用本脚本确认 goods_id / 详情是否真的落库了。
**只读**，不改任何数据。

Usage:
    python scripts/check_deep_harvest.py            # 默认看最近 5 条 deep run
    python scripts/check_deep_harvest.py -n 10      # 看更多
    python scripts/check_deep_harvest.py -k 电动牙刷  # 只看某关键词

三段输出：
    [1] 最近 deep run        —— run 的 items 里有几条带 goods_id / detail（深抓成功标志）
    [2] pdd_goods            —— 商品级详情是否落库（店铺/评论/规格…）
    [3] product_sightings    —— 观测表是否关联到 goods_id / 拿到 sold_count / coupon_price

判读：
    [1] goods_id 计数 > 0  →  worker 真进了详情页并拿到 id
    [2] 有行                →  详情成功 upsert 到 pdd_goods
    [3] goods_id 非空       →  观测表与商品已打通（前端「详」面板的数据源）
若 [1] 有 goods_id 但 [2]/[3] 空：多半 migration 没 upgrade（pdd_goods 表不存在），
或落库管线报错——查后端日志 `_record_pdd_sightings` / `upsert_pdd_goods`。
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncpg

from app.core.config import get_settings


def _fmt(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%m-%d %H:%M")


def _as_list(items_raw) -> list:
    """pdd_search_runs.items 是 jsonb；asyncpg 默认给 str，需 json.loads。"""
    if items_raw is None:
        return []
    if isinstance(items_raw, (list, dict)):
        return items_raw if isinstance(items_raw, list) else []
    try:
        parsed = json.loads(items_raw)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


async def _run(limit: int, keyword: str | None) -> None:
    raw_url = get_settings().DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    conn = await asyncpg.connect(raw_url)
    try:
        # ── [1] 最近 deep run ────────────────────────────────────
        if keyword:
            runs = await conn.fetch(
                """
                SELECT keyword_text, mode, status, items_count, items, created_at
                FROM pdd_search_runs
                WHERE mode = 'deep' AND keyword_text = $2
                ORDER BY created_at DESC LIMIT $1
                """,
                limit, keyword,
            )
        else:
            runs = await conn.fetch(
                """
                SELECT keyword_text, mode, status, items_count, items, created_at
                FROM pdd_search_runs
                WHERE mode = 'deep'
                ORDER BY created_at DESC LIMIT $1
                """,
                limit,
            )

        print("\n[1] 最近 deep run（gid=带 goods_id 条数 / det=带 detail 条数）")
        hdr = f"{'created':<12}{'keyword':<20}{'status':<8}{'items':>6}{'gid':>5}{'det':>5}"
        print(hdr)
        print("-" * len(hdr))
        if not runs:
            print("（无 deep run。先用前端「PDD深度搜」或跑一个 list_deep 关键词）")
        for r in runs:
            items = _as_list(r["items"])
            n_gid = sum(1 for it in items if isinstance(it, dict) and it.get("goods_id"))
            n_det = sum(1 for it in items if isinstance(it, dict) and it.get("detail"))
            print(
                f"{_fmt(r['created_at']):<12}"
                f"{(r['keyword_text'] or '')[:19]:<20}"
                f"{r['status']:<8}"
                f"{r['items_count']:>6}"
                f"{n_gid:>5}"
                f"{n_det:>5}"
            )

        # ── [2] pdd_goods ────────────────────────────────────────
        goods = await conn.fetch(
            """
            SELECT goods_id, shop_name, comment_count, praise_rate, discount,
                   rank_badges, specs, last_title, last_harvested_at
            FROM pdd_goods
            ORDER BY last_harvested_at DESC LIMIT $1
            """,
            limit,
        )
        print("\n[2] pdd_goods（商品级详情落库）")
        if not goods:
            print("（空。若 [1] 有 gid 但这里空 → 多半 alembic 没 upgrade head，pdd_goods 表不存在）")
        for g in goods:
            specs = _as_list(g["specs"]) if isinstance(g["specs"], str) else g["specs"]
            n_spec = len(specs) if isinstance(specs, dict) else 0
            print(
                f"  {g['goods_id']:<14} "
                f"店铺={g['shop_name'] or '—'!s:<16.16} "
                f"评论={g['comment_count'] if g['comment_count'] is not None else '—'!s:<8} "
                f"好评={g['praise_rate'] if g['praise_rate'] is not None else '—'!s:<6} "
                f"立减={g['discount'] if g['discount'] is not None else '—'!s:<6} "
                f"规格×{n_spec}  {_fmt(g['last_harvested_at'])}"
            )
            if g["last_title"]:
                print(f"      └ {g['last_title'][:50]}")

        # ── [3] product_sightings 关联 goods_id ──────────────────
        sights = await conn.fetch(
            """
            SELECT item_key, goods_id, sold_count, coupon_price, title, seen_date
            FROM product_sightings
            WHERE goods_id IS NOT NULL
            ORDER BY seen_date DESC, updated_at DESC LIMIT $1
            """,
            limit,
        )
        print("\n[3] product_sightings（观测表已关联 goods_id 的）")
        if not sights:
            print("（空。深抓的商品回流后这里才会带 goods_id；fast 词不会有）")
        for s in sights:
            print(
                f"  {s['seen_date']!s:<11} "
                f"gid={s['goods_id']:<14} "
                f"已拼={s['sold_count'] if s['sold_count'] is not None else '—'!s:<8} "
                f"券后={s['coupon_price'] if s['coupon_price'] is not None else '—'!s:<7} "
                f"{(s['title'] or '')[:34]}"
            )
        print()
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-n", type=int, default=5, help="每段最多打印几行（默认 5）")
    parser.add_argument("-k", "--keyword", help="只看某关键词的 deep run")
    args = parser.parse_args()

    os.environ.setdefault(
        "DATABASE_URL",
        os.environ.get("DATABASE_URL")
        or "postgresql+asyncpg://postgres:postgres@localhost:5432/xianyu",
    )
    asyncio.run(_run(max(1, args.n), args.keyword))


if __name__ == "__main__":
    main()
