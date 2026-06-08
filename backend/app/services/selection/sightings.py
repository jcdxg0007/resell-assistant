"""跨天「同款观测」服务（Phase 1，精确指纹 L1）。

写入：采集成功落库后调 record_sightings()，把每条商品按稳定指纹 item_key 在当前
逻辑日 upsert 一条（一天一条）。读：gather_sighting_stats() 给一批 item_key 算
「首次/最近出现、出现天数、每日价格/热度序列」，供十维度选品页做标签与趋势图。

身份指纹：
  - 闲鱼：xy:<source_id>（闲鱼 item_id 稳定）
  - PDD ：pdd:<sha1(clean_title)[:32]>（无稳定 id，用归一化标题指纹，跨关键词也能合并）

设计成「绝不抛异常」：写入失败只记日志，不连累采集主流程。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date
from typing import Any, Iterable

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product_sighting import ProductSighting
from app.models.pdd_goods import PddGoods
from app.services.pdd_search_run import _cn_day_start
from app.services.selection.matcher import clean_title

logger = logging.getLogger(__name__)

# 单个商品的价格/热度历史最多回看多少天（前端 sparkline 用，避免 payload 过大）
_MAX_HISTORY_POINTS = 30


def xianyu_item_key(source_id: str | None) -> str | None:
    sid = (source_id or "").strip()
    return f"xy:{sid}" if sid else None


def pdd_item_key(title: str | None) -> str | None:
    norm = clean_title(title or "").lower().strip()
    if not norm:
        return None
    return "pdd:" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:32]


def _today_logical_date() -> date:
    """当前逻辑日（东八 3 点日界）的日期。"""
    return _cn_day_start().date()


async def record_sightings(
    db: AsyncSession,
    platform: str,
    records: Iterable[dict[str, Any]],
    *,
    keyword: str | None = None,
    commit: bool = True,
) -> int:
    """把一批观测 upsert 到 product_sightings（按 item_key+逻辑日，一天一条）。

    records: 每条 dict 需含 item_key；可选 title/price/heat/image_url，以及深度
    收割补充的 goods_id/sold_count/coupon_price（Step 3）。
    返回成功写入的条数（去重后）。绝不抛异常。

    注意 on_conflict 更新时，goods_id/sold_count/coupon_price 用 COALESCE(新, 旧)：
    同一逻辑日先 list-level 落（无 goods_id）、后 dip 补（有 goods_id）时不被抹掉。
    """
    seen = _today_logical_date()
    written = 0
    try:
        for r in records:
            key = r.get("item_key")
            if not key:
                continue
            stmt = pg_insert(ProductSighting).values(
                platform=platform,
                item_key=key,
                seen_date=seen,
                keyword=keyword,
                title=r.get("title"),
                price=r.get("price"),
                heat=r.get("heat"),
                image_url=r.get("image_url"),
                goods_id=r.get("goods_id"),
                sold_count=r.get("sold_count"),
                coupon_price=r.get("coupon_price"),
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_sightings_key_date",
                set_={
                    "price": r.get("price"),
                    "heat": r.get("heat"),
                    "title": r.get("title"),
                    "image_url": r.get("image_url"),
                    "keyword": keyword,
                    # 已有值就保留，避免后续 list-level 刷新把 dip 补的字段清空
                    "goods_id": func.coalesce(stmt.excluded.goods_id, ProductSighting.goods_id),
                    "sold_count": func.coalesce(stmt.excluded.sold_count, ProductSighting.sold_count),
                    "coupon_price": func.coalesce(stmt.excluded.coupon_price, ProductSighting.coupon_price),
                    "updated_at": func.now(),
                },
            )
            await db.execute(stmt)
            written += 1
        if commit:
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — 观测落库失败不应连累采集
        logger.warning(f"record_sightings({platform}) failed: {exc}")
        try:
            await db.rollback()
        except Exception:
            pass
        return 0
    return written


async def upsert_pdd_goods(
    db: AsyncSession, goods: Iterable[dict[str, Any]], *, commit: bool = True
) -> int:
    """把一批深度收割到的商品级详情 upsert 到 pdd_goods（按 goods_id）。

    goods: 每条需含 goods_id；可选 shop_name/comment_count/praise_rate/rank_badges/
    review_tags/specs/discount/thumb_url/detail_url/last_title/last_price。
    再次收割同一 goods_id 则刷新「最新」详情 + last_harvested_at（COALESCE 保留旧非空）。
    绝不抛异常。
    """
    written = 0
    try:
        for g in goods:
            gid = (g.get("goods_id") or "").strip()
            if not gid:
                continue
            vals = dict(
                goods_id=gid,
                shop_name=g.get("shop_name"),
                comment_count=g.get("comment_count"),
                praise_rate=g.get("praise_rate"),
                rank_badges=g.get("rank_badges"),
                review_tags=g.get("review_tags"),
                specs=g.get("specs"),
                discount=g.get("discount"),
                thumb_url=g.get("thumb_url"),
                detail_url=g.get("detail_url"),
                last_title=g.get("last_title") or g.get("title"),
                last_price=g.get("last_price") if g.get("last_price") is not None
                else g.get("price"),
            )
            stmt = pg_insert(PddGoods).values(**vals)
            # 新值优先，新值为空则保留旧值；时间戳刷新
            set_ = {
                k: func.coalesce(getattr(stmt.excluded, k), getattr(PddGoods, k))
                for k in vals if k != "goods_id"
            }
            set_["last_harvested_at"] = func.now()
            stmt = stmt.on_conflict_do_update(index_elements=["goods_id"], set_=set_)
            await db.execute(stmt)
            written += 1
        if commit:
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — 详情落库失败不应连累采集
        logger.warning(f"upsert_pdd_goods failed: {exc}")
        try:
            await db.rollback()
        except Exception:
            pass
        return 0
    return written


async def gather_pdd_goods(
    db: AsyncSession, goods_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """给一批 goods_id 取商品级详情（pdd_goods）。:return: {goods_id: {...字段}}。"""
    gids = [g for g in dict.fromkeys(goods_ids) if g]
    if not gids:
        return {}
    rows = (await db.execute(
        select(PddGoods).where(PddGoods.goods_id.in_(gids))
    )).scalars().all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out[r.goods_id] = {
            "shop_name": r.shop_name,
            "comment_count": r.comment_count,
            "praise_rate": r.praise_rate,
            "rank_badges": r.rank_badges,
            "review_tags": r.review_tags,
            "specs": r.specs,
            "discount": r.discount,
            "thumb_url": r.thumb_url,
            "detail_url": r.detail_url,
        }
    return out


async def gather_sighting_stats(
    db: AsyncSession, item_keys: list[str]
) -> dict[str, dict[str, Any]]:
    """给一批 item_key 算跨天统计。

    **goods_id 优先归并**（D2）：若某些观测带同一 goods_id，它们的跨天序列合并成一条
    （即使因卖家改标题导致 item_key 不同也能合并）；无 goods_id 时退回按 item_key。
    返回仍按请求的 item_key 索引（每个 key 映射到它所属分组的合并统计）。

    :return: {item_key: {first_seen, last_seen, days_seen, history:[{date,price,heat}]}}
             history 按日期升序，最多 _MAX_HISTORY_POINTS 个点。
    """
    keys = [k for k in dict.fromkeys(item_keys) if k]  # 去重保序、去空
    if not keys:
        return {}
    rows = (await db.execute(
        select(
            ProductSighting.item_key,
            ProductSighting.goods_id,
            ProductSighting.seen_date,
            ProductSighting.price,
            ProductSighting.heat,
        )
        .where(ProductSighting.item_key.in_(keys))
        .order_by(ProductSighting.seen_date.asc())
    )).all()

    # 第二跳：把这批里出现过的 goods_id 的**全部**观测也拉进来（可能落在别的
    # item_key 下），才能跨标题合并完整历史。无 goods_id 时此步为空、行为不变。
    gids = {g for _k, g, _d, _p, _h in rows if g}
    if gids:
        extra = (await db.execute(
            select(
                ProductSighting.item_key,
                ProductSighting.goods_id,
                ProductSighting.seen_date,
                ProductSighting.price,
                ProductSighting.heat,
            )
            .where(ProductSighting.goods_id.in_(gids))
            .order_by(ProductSighting.seen_date.asc())
        )).all()
        rows = list(rows) + list(extra)

    # 分组键：有 goods_id 用 goods_id，否则用 item_key
    # 同时记录每个请求 item_key 属于哪个分组
    key_group: dict[str, str] = {}
    groups: dict[str, dict[str, dict]] = {}  # group -> {date_iso: {price,heat}}
    for key, gid, seen_date, price, heat in rows:
        grp = f"g:{gid}" if gid else f"k:{key}"
        if key in keys:
            key_group[key] = grp
        d = seen_date.isoformat()
        # 同分组同日只留一条（后出现的覆盖，行已按日期升序）
        groups.setdefault(grp, {})[d] = {"date": d, "price": price, "heat": heat}

    out: dict[str, dict[str, Any]] = {}
    for key in keys:
        grp = key_group.get(key)
        if not grp or grp not in groups:
            continue
        hist = [groups[grp][d] for d in sorted(groups[grp].keys())]
        entry = {
            "history": hist[-_MAX_HISTORY_POINTS:],
            "first_seen": hist[0]["date"],
            "last_seen": hist[-1]["date"],
            "days_seen": len(hist),
        }
        out[key] = entry
    return out
