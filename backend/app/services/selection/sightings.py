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

    records: 每条 dict 需含 item_key；可选 title/price/heat/image_url。
    返回成功写入的条数（去重后）。绝不抛异常。
    """
    seen = _today_logical_date()
    written = 0
    try:
        for r in records:
            key = r.get("item_key")
            if not key:
                continue
            stmt = (
                pg_insert(ProductSighting)
                .values(
                    platform=platform,
                    item_key=key,
                    seen_date=seen,
                    keyword=keyword,
                    title=r.get("title"),
                    price=r.get("price"),
                    heat=r.get("heat"),
                    image_url=r.get("image_url"),
                )
                .on_conflict_do_update(
                    constraint="uq_sightings_key_date",
                    set_={
                        "price": r.get("price"),
                        "heat": r.get("heat"),
                        "title": r.get("title"),
                        "image_url": r.get("image_url"),
                        "keyword": keyword,
                        "updated_at": func.now(),
                    },
                )
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


async def gather_sighting_stats(
    db: AsyncSession, item_keys: list[str]
) -> dict[str, dict[str, Any]]:
    """给一批 item_key 算跨天统计。

    :return: {item_key: {first_seen, last_seen, days_seen, history:[{date,price,heat}]}}
             history 按日期升序，最多 _MAX_HISTORY_POINTS 个点。
    """
    keys = [k for k in dict.fromkeys(item_keys) if k]  # 去重保序、去空
    if not keys:
        return {}
    rows = (await db.execute(
        select(
            ProductSighting.item_key,
            ProductSighting.seen_date,
            ProductSighting.price,
            ProductSighting.heat,
        )
        .where(ProductSighting.item_key.in_(keys))
        .order_by(ProductSighting.item_key, ProductSighting.seen_date.asc())
    )).all()

    out: dict[str, dict[str, Any]] = {}
    for key, seen_date, price, heat in rows:
        entry = out.setdefault(key, {"history": []})
        entry["history"].append({
            "date": seen_date.isoformat(),
            "price": price,
            "heat": heat,
        })
    for key, entry in out.items():
        hist = entry["history"]
        entry["first_seen"] = hist[0]["date"]
        entry["last_seen"] = hist[-1]["date"]
        entry["days_seen"] = len(hist)
        # 只保留最近 N 个点给前端
        if len(hist) > _MAX_HISTORY_POINTS:
            entry["history"] = hist[-_MAX_HISTORY_POINTS:]
    return out
