"""十维度选品打分（解耦版）。

把打分从闲鱼采集链路（_instant_search）里独立出来，给「十维度选品」页专用。
三层：
  A. 闲鱼端  —— 评「这条闲鱼挂牌本身好不好」（满分 100）
  B. PDD 端  —— A 的镜像，热度信号换成 PDD 销量，平台特有项换成货源正规度
  C. 跨平台套利 —— 对比两端价格分布，自动判方向（贵端=卖出端）+ 算利润 + 推荐度

本模块是纯函数：不碰 DB、不碰网络。调用方（API）负责把已采集的原始数据
（闲鱼 products 行 / PDD pdd_search_runs.items）取出来喂进来，并把返回的 dict
落到 selection_analysis 缓存表。

点数分配见 docs / 计划文件；后续可在前端/配置里调，故集中放在 _A_DIMS / _B_DIMS。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any


def pdd_fingerprint(keyword: str | None, title: str | None) -> str:
    """PDD 快照的稳定指纹：sha1(keyword|title)[:32]。

    PDD 端商品无稳定 id，用「关键词+标题」做内容指纹，给前端当 product_id
    （pdd:<fp>）做收藏开关，也给后端 pdd_pins 去重/匹配。
    """
    raw = f"{(keyword or '').strip()}|{(title or '').strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]

from app.services.selection.data_cleaning import (
    PriceStats, clean_keyword_sample, tokenize_keyword, calculate_relevance,
)
from app.services.selection.product_scoring import (
    _score_title_quality, _score_price_zone,
)

# 成本估算参数（与 app.core.config 默认一致；这里独立常量避免 import settings 依赖）
_LOGISTICS_COST = 3.5
_LOSS_RATE = 1.05

# 货源正规度关键词
_OFFICIAL_BADGE = re.compile(r"(百亿补贴|旗舰店|官方|品牌|正品|授权|自营)")
_SUBSIDY_BADGE = re.compile(r"(百亿补贴)")


# ──────────────────────────────────────────
# 维度定义（name, max_score）
# ──────────────────────────────────────────
_A_DIMS = [
    ("价格竞争力", 20.0),
    ("单品热度", 25.0),
    ("商品相关性", 15.0),
    ("标题质量", 10.0),
    ("客单价区间", 10.0),
    ("闲鱼竞争度", 10.0),
    ("图片质量", 5.0),
    ("挂牌新鲜度", 5.0),
]
_B_DIMS = [
    ("价格竞争力", 20.0),
    ("单品热度", 25.0),
    ("商品相关性", 15.0),
    ("标题质量", 10.0),
    ("客单价区间", 10.0),
    ("货源正规度", 10.0),
    ("图片质量", 10.0),
]

# PDD worker 上报的 item 是自由 dict，图片字段名不固定，探测多种常见 key。
_PDD_IMAGE_KEYS = (
    "image", "image_url", "thumbnail", "thumb_url",
    "goods_thumbnail_url", "hd_thumb_url", "goods_image",
)

SIDE_DECISION_LABELS = {
    "buy": "推荐",
    "watch": "观察",
    "skip": "跳过",
}
ARB_DECISION_LABELS = {
    "strong": "强烈推荐",
    "try": "可尝试",
    "skip": "不建议",
}
DIRECTION_LABELS = {
    "pdd_to_xianyu": "PDD进货 → 闲鱼卖",
    "xianyu_to_pdd": "闲鱼收货 → PDD卖",
}


def _dim(name: str, frac: float, max_score: float, label: str, has_data: bool = True) -> dict[str, Any]:
    frac = max(0.0, min(1.0, frac))
    return {
        "name": name,
        "score": round(frac * max_score, 1),
        "max": max_score,
        "label": label,
        "has_data": has_data,
    }


# ──────────────────────────────────────────
# 单维度打分（返回 0~1 的 frac + 文案）
# ──────────────────────────────────────────

def _price_comp_frac(price: float, median: float, suspicious: bool) -> tuple[float, str]:
    """甜蜜区 U 曲线：本价 vs 同平台同词中位价。"""
    if suspicious:
        return 0.13, "可疑极低价"
    if median <= 0 or price <= 0:
        return 0.5, "无法比较"
    ratio = price / median
    if ratio < 0.4:
        return 0.13, "可疑极低价"
    if ratio < 0.6:
        return 0.53, "低得较多"
    if ratio < 0.8:
        return 1.0, "甜蜜区"
    if ratio < 1.0:
        return 0.8, "略低于主流"
    if ratio < 1.2:
        return 0.53, "接近主流价"
    if ratio < 1.5:
        return 0.27, "偏贵"
    return 0.07, "远高于主流"


def _xianyu_heat_frac(wants: int) -> tuple[float, str]:
    if wants >= 50:
        return 1.0, "单品火爆"
    if wants >= 20:
        return 0.8, "需求好"
    if wants >= 10:
        return 0.6, "有需求"
    if wants >= 3:
        return 0.4, "有人想要"
    if wants >= 1:
        return 0.25, "少量想要"
    return 0.1, "无人问津"


def _pdd_heat_frac(sales: int) -> tuple[float, str]:
    if sales >= 5000:
        return 1.0, "爆款"
    if sales >= 1000:
        return 0.85, "热销"
    if sales >= 300:
        return 0.7, "销量好"
    if sales >= 100:
        return 0.55, "有销量"
    if sales >= 10:
        return 0.4, "少量成交"
    if sales >= 1:
        return 0.25, "零星成交"
    return 0.1, "无销量"


def _competition_frac(active_listings: int | None) -> tuple[float, str, bool]:
    """闲鱼竞争度：同词在卖挂牌数越多 = 红海 = 减分。"""
    if active_listings is None or active_listings <= 0:
        return 0.5, "无供给数据", False
    al = active_listings
    if al <= 20:
        return 1.0, "蓝海", True
    if al <= 50:
        return 0.8, "竞争较小", True
    if al <= 150:
        return 0.6, "竞争适中", True
    if al <= 400:
        return 0.4, "竞争较大", True
    return 0.2, "红海", True


def _supply_quality_frac(badges: list[str] | None, sales: int) -> tuple[float, str]:
    """PDD 货源正规度：百亿补贴/旗舰店/品牌 + 销量规模。"""
    text = " ".join(badges or [])
    frac = 0.4
    bits: list[str] = []
    if _SUBSIDY_BADGE.search(text):
        frac += 0.3
        bits.append("百亿补贴")
    if _OFFICIAL_BADGE.search(text) and "百亿补贴" not in bits:
        frac += 0.2
        bits.append("旗舰/官方")
    if sales >= 100:
        frac += 0.1
        bits.append("有销量")
    if not bits:
        return 0.3, "普通货源"
    return min(frac, 1.0), " ".join(bits)


def _image_quality_frac(image_count: int) -> tuple[float, str, bool]:
    """图片质量：列表页一般只有 1 张主图，故主要区分有图/无图；多图留给详情页。"""
    if image_count <= 0:
        return 0.2, "无主图", True
    if image_count >= 3:
        return 1.0, "多图", True
    return 0.7, "有主图", True


def _freshness_frac(published_at_iso: str | None) -> tuple[float, str, bool]:
    """挂牌新鲜度：发布越近越好（二手转卖里新挂牌通常更有效、更易成交）。"""
    if not published_at_iso:
        return 0.5, "无发布时间", False
    try:
        dt = datetime.fromisoformat(published_at_iso)
    except (ValueError, TypeError):
        return 0.5, "无发布时间", False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    if days <= 3:
        return 1.0, "刚上架", True
    if days <= 7:
        return 0.85, "近一周", True
    if days <= 30:
        return 0.6, "近一月", True
    if days <= 90:
        return 0.35, "较久", True
    return 0.15, "陈旧挂牌", True


def _pdd_image_count(item: dict[str, Any]) -> int:
    for k in _PDD_IMAGE_KEYS:
        v = item.get(k)
        if v:
            return len(v) if isinstance(v, list) else 1
    return 0


def _pdd_image_url(item: dict[str, Any]) -> str | None:
    """取 PDD item 的首图地址（worker 截屏裁的是 data:image/... 的 base64）。"""
    for k in _PDD_IMAGE_KEYS:
        v = item.get(k)
        if not v:
            continue
        if isinstance(v, list):
            return v[0] if v else None
        return v
    return None


def _profit_margin_frac(margin_pct: float) -> tuple[float, str]:
    if margin_pct > 40:
        return 1.0, "高利润"
    if margin_pct > 25:
        return 0.73, "利润较好"
    if margin_pct > 15:
        return 0.47, "薄利"
    if margin_pct > 5:
        return 0.2, "微利"
    return 0.07, "利润过低"


def _gap_frac(ratio: float) -> tuple[float, str]:
    """价差幅度：卖出端中位价 / 进货端中位价。"""
    if ratio >= 2.0:
        return 1.0, "价差悬殊"
    if ratio >= 1.6:
        return 0.85, "价差大"
    if ratio >= 1.3:
        return 0.65, "价差明显"
    if ratio >= 1.15:
        return 0.4, "价差一般"
    if ratio >= 1.0:
        return 0.2, "价差很小"
    return 0.05, "无价差"


def _decision_side(total: float) -> str:
    if total >= 75:
        return "buy"
    if total >= 55:
        return "watch"
    return "skip"


# ──────────────────────────────────────────
# A. 闲鱼端
# ──────────────────────────────────────────

def score_xianyu_side(
    keyword: str,
    items: list[dict[str, Any]],
    *,
    active_listings: int | None = None,
) -> dict[str, Any]:
    """items: [{product_id, title, price, item_wants, image_urls?, published_at?}]"""
    orig_by_id: dict[str, dict[str, Any]] = {
        str(it.get("product_id")): it for it in items if it.get("product_id")
    }
    cleaned, single_stats, _suite = clean_keyword_sample(items, keyword)
    median = single_stats.median

    ranked: list[dict[str, Any]] = []
    for cp in cleaned:
        orig = orig_by_id.get(cp.product_id, {})
        image_urls = orig.get("image_urls") or []
        published_at = orig.get("published_at")

        dims: list[dict[str, Any]] = []
        f, lbl = _price_comp_frac(cp.price, median, cp.is_suspicious_low)
        dims.append(_dim("价格竞争力", f, 20.0, lbl, has_data=(median > 0)))
        f, lbl = _xianyu_heat_frac(cp.item_wants)
        dims.append(_dim("单品热度", f, 25.0, lbl))
        dims.append(_dim("商品相关性", cp.relevance_score / 10.0, 15.0, _relevance_label(cp.relevance_score)))
        ts, tl = _score_title_quality(cp.title)
        dims.append(_dim("标题质量", ts / 10.0, 10.0, tl))
        ps, pl = _score_price_zone(cp.price)
        dims.append(_dim("客单价区间", ps / 10.0, 10.0, pl))
        cf, cl, chas = _competition_frac(active_listings)
        dims.append(_dim("闲鱼竞争度", cf, 10.0, cl, has_data=chas))
        imgf, imgl, imghas = _image_quality_frac(len(image_urls) if isinstance(image_urls, list) else 0)
        dims.append(_dim("图片质量", imgf, 5.0, imgl, has_data=imghas))
        frf, frl, frhas = _freshness_frac(published_at)
        dims.append(_dim("挂牌新鲜度", frf, 5.0, frl, has_data=frhas))

        total = round(sum(d["score"] for d in dims), 1)
        ranked.append({
            "product_id": cp.product_id,
            "title": cp.title,
            "price": cp.price,
            "item_wants": cp.item_wants,
            "image_url": (image_urls[0] if isinstance(image_urls, list) and image_urls else None),
            "source_url": orig.get("source_url"),
            "seller_name": orig.get("seller_name"),
            "source_id": orig.get("source_id"),
            "published_at": published_at,
            "crawled_at": orig.get("crawled_at"),
            "relevance": cp.relevance_score,
            "risk_tags": cp.risk_tags,
            "total_score": total,
            "decision": _decision_side(total),
            "decision_label": SIDE_DECISION_LABELS[_decision_side(total)],
            "dimensions": dims,
        })

    ranked.sort(key=lambda x: x["total_score"], reverse=True)
    return {
        "platform": "xianyu",
        "median": median,
        "p25": single_stats.p25,
        "p75": single_stats.p75,
        "sample_size": single_stats.sample_size,
        "active_listings": active_listings,
        "items": ranked,
    }


# ──────────────────────────────────────────
# B. PDD 端
# ──────────────────────────────────────────

def score_pdd_side(keyword: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """items: PDD worker 原始结构 [{title, price, sales, badges, ...}]（无 product_id）。"""
    # 合成 product_id 喂清洗；item_wants 借位塞 sales 让 cleaned 带过去。
    raw = []
    orig_by_id: dict[str, dict[str, Any]] = {}
    for idx, it in enumerate(items):
        pid = f"{keyword}#{idx}"
        raw.append({
            "product_id": pid,
            "title": it.get("title") or "",
            "price": float(it.get("price") or 0),
            "item_wants": int(it.get("sales") or 0),
        })
        orig_by_id[pid] = it

    cleaned, single_stats, _suite = clean_keyword_sample(raw, keyword)
    median = single_stats.median

    ranked: list[dict[str, Any]] = []
    for cp in cleaned:
        orig = orig_by_id.get(cp.product_id, {})
        sales = int(orig.get("sales") or 0)
        badges = orig.get("badges") or []

        dims: list[dict[str, Any]] = []
        f, lbl = _price_comp_frac(cp.price, median, cp.is_suspicious_low)
        dims.append(_dim("价格竞争力", f, 20.0, lbl, has_data=(median > 0)))
        f, lbl = _pdd_heat_frac(sales)
        dims.append(_dim("单品热度", f, 25.0, lbl))
        dims.append(_dim("商品相关性", cp.relevance_score / 10.0, 15.0, _relevance_label(cp.relevance_score)))
        ts, tl = _score_title_quality(cp.title)
        dims.append(_dim("标题质量", ts / 10.0, 10.0, tl))
        ps, pl = _score_price_zone(cp.price)
        dims.append(_dim("客单价区间", ps / 10.0, 10.0, pl))
        sf, sl = _supply_quality_frac(badges, sales)
        dims.append(_dim("货源正规度", sf, 10.0, sl))
        imgf, imgl, imghas = _image_quality_frac(_pdd_image_count(orig))
        dims.append(_dim("图片质量", imgf, 10.0, imgl, has_data=imghas))

        total = round(sum(d["score"] for d in dims), 1)
        ranked.append({
            "product_id": f"pdd:{pdd_fingerprint(keyword, cp.title)}",
            "title": cp.title,
            "price": cp.price,
            "sales": sales,
            "badges": badges,
            "image_url": _pdd_image_url(orig),
            "crawled_at": orig.get("crawled_at"),
            # 深度收割商品才有；供 _attach_sighting_stats 按 goods_id 挂详情
            "goods_id": orig.get("goods_id"),
            "relevance": cp.relevance_score,
            "risk_tags": cp.risk_tags,
            "total_score": total,
            "decision": _decision_side(total),
            "decision_label": SIDE_DECISION_LABELS[_decision_side(total)],
            "dimensions": dims,
        })

    ranked.sort(key=lambda x: x["total_score"], reverse=True)
    return {
        "platform": "pdd",
        "median": median,
        "p25": single_stats.p25,
        "p75": single_stats.p75,
        "sample_size": single_stats.sample_size,
        "items": ranked,
    }


# ──────────────────────────────────────────
# C. 跨平台套利
# ──────────────────────────────────────────

def compute_arbitrage(
    xianyu_side: dict[str, Any],
    pdd_side: dict[str, Any],
) -> dict[str, Any]:
    """对比两端价格分布，自动判方向并算套利分。两端都得有数据才出结论。"""
    xy_med = float(xianyu_side.get("median") or 0)
    pdd_med = float(pdd_side.get("median") or 0)
    xy_n = int(xianyu_side.get("sample_size") or 0)
    pdd_n = int(pdd_side.get("sample_size") or 0)

    if xy_med <= 0 or pdd_med <= 0 or xy_n == 0 or pdd_n == 0:
        return {"available": False, "reason": "两端价格样本不足，无法比价"}

    # 贵的那端 = 卖出端，便宜的那端 = 进货端
    if xy_med >= pdd_med:
        direction = "pdd_to_xianyu"
        sell_price, source_cost = xy_med, pdd_med
        sell_items = xianyu_side.get("items") or []
        sell_signal = "wants"
    else:
        direction = "xianyu_to_pdd"
        sell_price, source_cost = pdd_med, xy_med
        sell_items = pdd_side.get("items") or []
        sell_signal = "sales"

    dims: list[dict[str, Any]] = []

    # 1) 价差幅度 30
    ratio = sell_price / source_cost if source_cost > 0 else 0
    gf, gl = _gap_frac(ratio)
    dims.append(_dim("价差幅度", gf, 30.0, f"{gl} ({ratio:.2f}x)"))

    # 2) 套利利润率 30
    cost = source_cost * _LOSS_RATE + _LOGISTICS_COST
    est_profit = round(sell_price - cost, 1)
    margin_pct = (sell_price - cost) / sell_price * 100.0 if sell_price > 0 else 0
    mf, ml = _profit_margin_frac(margin_pct)
    dims.append(_dim("套利利润率", mf, 30.0, f"{ml} ({margin_pct:.0f}%)"))

    # 3) 需求侧强度 20（看卖出端的成交信号中位）
    demand_vals = sorted(
        int(it.get("item_wants" if sell_signal == "wants" else "sales") or 0)
        for it in sell_items
    )
    demand_med = demand_vals[len(demand_vals) // 2] if demand_vals else 0
    if sell_signal == "wants":
        df, dl = _xianyu_heat_frac(demand_med)
    else:
        df, dl = _pdd_heat_frac(demand_med)
    dims.append(_dim("需求侧强度", df, 20.0, f"{dl} (中位 {demand_med})"))

    # 4) 可行性/风险 20
    feas = 1.0
    notes: list[str] = []
    if min(xy_n, pdd_n) < 5:
        feas -= 0.4
        notes.append("样本偏少")
    susp_xy = sum(1 for it in (xianyu_side.get("items") or []) if "suspicious_low" in (it.get("risk_tags") or []))
    susp_pdd = sum(1 for it in (pdd_side.get("items") or []) if "suspicious_low" in (it.get("risk_tags") or []))
    if susp_xy + susp_pdd >= 3:
        feas -= 0.25
        notes.append("钓鱼极低价多")
    rel_all = [it.get("relevance", 0) for it in (xianyu_side.get("items") or []) + (pdd_side.get("items") or [])]
    rel_avg = (sum(rel_all) / len(rel_all)) if rel_all else 0
    if rel_avg < 5:
        feas -= 0.2
        notes.append("同款匹配弱")
    feas = max(0.0, feas)
    dims.append(_dim("可行性/风险", feas, 20.0, " ".join(notes) or "可行"))

    total = round(sum(d["score"] for d in dims), 1)
    if total >= 70:
        decision = "strong"
    elif total >= 50:
        decision = "try"
    else:
        decision = "skip"

    return {
        "available": True,
        "direction": direction,
        "direction_label": DIRECTION_LABELS[direction],
        "sell_price": round(sell_price, 1),
        "source_cost": round(source_cost, 1),
        "estimated_cost": round(cost, 1),
        "estimated_profit": est_profit,
        "profit_margin": round(margin_pct, 1),
        "total_score": total,
        "decision": decision,
        "decision_label": ARB_DECISION_LABELS[decision],
        "dimensions": dims,
    }


def _relevance_label(r: float) -> str:
    if r >= 8.0:
        return "高度相关"
    if r >= 6.0:
        return "相关"
    if r >= 4.0:
        return "部分相关"
    return "相关性低"


def analyze(
    keyword: str,
    *,
    xianyu_items: list[dict[str, Any]],
    pdd_items: list[dict[str, Any]],
    active_listings: int | None = None,
) -> dict[str, Any]:
    """一站式：A + B + C，返回可直接落库的 payload。"""
    xianyu_side = score_xianyu_side(keyword, xianyu_items, active_listings=active_listings)
    pdd_side = score_pdd_side(keyword, pdd_items)
    arbitrage = compute_arbitrage(xianyu_side, pdd_side)
    return {
        "keyword": keyword,
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "xianyu": xianyu_side,
        "pdd": pdd_side,
        "arbitrage": arbitrage,
    }
