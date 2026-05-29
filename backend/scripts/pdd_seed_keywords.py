"""批量入种 PDD 词库种子词。

把一批关键词插入 selection_keywords 表，标记 pdd_safe=TRUE 让
``pdd_fire_from_lib.py`` 能自动轮播跑批。

用法（在 Sealos backend pod 内）::

    # 单类入库（多个词用空格分隔）
    python -m scripts.pdd_seed_keywords \\
        --category "DIY手工" \\
        --slug diy \\
        --hint "木工/手工/模型/手作材料" \\
        --mode fast \\
        木条 木板 雪糕棒 松木条 桐木板

    # 列出现有词库
    python -m scripts.pdd_seed_keywords --list

    # 把某词标记为 不安全（永久禁用 PDD 调度）
    python -m scripts.pdd_seed_keywords --disable 美瞳

不重复入库：如果 (category, text) 已存在，跳过（不覆盖任何字段）。
如果 category 不存在，自动建一个。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import select  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.models.selection import Category, Keyword  # noqa: E402


async def _get_or_create_category(
    db, name: str, slug: str, hint: str | None
) -> Category:
    cat = (
        await db.execute(select(Category).where(Category.slug == slug))
    ).scalar_one_or_none()
    if cat:
        return cat
    cat = Category(
        id=str(uuid.uuid4()),
        name=name,
        slug=slug,
        niche_hint=hint,
        is_active=True,
    )
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    print(f"  [+] 新建 category: {name} (slug={slug})")
    return cat


async def _insert_seeds(
    category_name: str, slug: str, hint: str | None,
    mode: str, keywords: list[str],
) -> None:
    async with AsyncSessionLocal() as db:
        cat = await _get_or_create_category(db, category_name, slug, hint)
        added, skipped = 0, 0
        for kw_text in keywords:
            kw_text = kw_text.strip()
            if not kw_text:
                continue
            existing = (
                await db.execute(
                    select(Keyword)
                    .where(Keyword.category_id == cat.id)
                    .where(Keyword.text == kw_text)
                )
            ).scalar_one_or_none()
            if existing:
                # 已存在：补 PDD 字段（如果旧的没设过）
                changed = False
                if not existing.pdd_mode or existing.pdd_mode == "fast":
                    if existing.pdd_mode != mode:
                        existing.pdd_mode = mode
                        changed = True
                if changed:
                    print(f"  [~] '{kw_text}' 已存在，更新 pdd_mode={mode}")
                else:
                    print(f"  [-] '{kw_text}' 已存在，跳过")
                skipped += 1
                continue
            kw = Keyword(
                id=str(uuid.uuid4()),
                category_id=cat.id,
                text=kw_text,
                target_platforms=["pdd"],  # 仅 PDD（其他平台单独配置）
                max_items_per_platform=30,
                schedule_enabled=True,
                is_active=True,
                pdd_mode=mode,
                pdd_safe=True,
                pdd_searches_total=0,
            )
            db.add(kw)
            print(f"  [+] '{kw_text}'  mode={mode}")
            added += 1
        await db.commit()
        print(f"\n小结: 新增 {added}，跳过 {skipped}")


async def _list_all() -> None:
    async with AsyncSessionLocal() as db:
        cats = (await db.execute(select(Category))).scalars().all()
        for c in cats:
            print(f"\n[{c.name}]  slug={c.slug}  active={c.is_active}")
            kws = (
                await db.execute(
                    select(Keyword)
                    .where(Keyword.category_id == c.id)
                    .order_by(Keyword.pdd_last_searched_at.asc().nullsfirst())
                )
            ).scalars().all()
            if not kws:
                print("  (无词)")
                continue
            for k in kws:
                safe = "🟢" if k.pdd_safe else "🚫"
                tags = []
                if not k.schedule_enabled:
                    tags.append("disabled")
                if not k.is_active:
                    tags.append("inactive")
                last = (
                    k.pdd_last_searched_at.strftime("%m-%d %H:%M")
                    if k.pdd_last_searched_at else "—— 未跑过 ——"
                )
                tag_str = f" [{','.join(tags)}]" if tags else ""
                print(
                    f"  {safe} {k.text:<24} mode={k.pdd_mode:<14} "
                    f"runs={k.pdd_searches_total:>3}  last={last}"
                    f"  status={k.pdd_last_status or '?'}{tag_str}"
                )


async def _disable(keyword: str) -> None:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(select(Keyword).where(Keyword.text == keyword))
        ).scalars().all()
        if not rows:
            print(f"❌ 没找到 '{keyword}'")
            return
        for k in rows:
            k.pdd_safe = False
            print(f"  [✗] disabled pdd '{k.text}' (id={k.id})")
        await db.commit()


async def main() -> int:
    p = argparse.ArgumentParser(description="PDD 词库种子词入库工具")
    p.add_argument("keywords", nargs="*", help="一个或多个种子词")
    p.add_argument("--category", default="PDD选品种子", help="分类名（中文）")
    p.add_argument(
        "--slug", default="pdd-seeds",
        help="分类 slug（英文小写连字符）"
    )
    p.add_argument("--hint", default=None, help="分类备注")
    p.add_argument(
        "--mode", default="fast",
        choices=("fast", "list_deep", "detail_smart", "detail_deep"),
        help="PDD 采集模式（默认 fast）"
    )
    p.add_argument("--list", action="store_true", help="列出现有词库")
    p.add_argument(
        "--disable", metavar="KEYWORD",
        help="把某词的 pdd_safe 改成 FALSE（永久禁用 PDD 调度）"
    )
    args = p.parse_args()

    if args.list:
        await _list_all()
        return 0
    if args.disable:
        await _disable(args.disable)
        return 0
    if not args.keywords:
        p.print_help()
        return 1
    await _insert_seeds(
        args.category, args.slug, args.hint, args.mode, args.keywords
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
