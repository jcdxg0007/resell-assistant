"""
Product-level scoring - 100-point scale, 8 dimensions.

Answers the question "should we resell this individual listing?"

Key behaviors:
  - Price competitiveness uses a sweet-spot U curve against a robust median
    (not avg), so bait-and-switch 9.9-yuan listings no longer top the chart.
  - Product relevance is its own scored dimension; callers still hard-filter
    at <40% before writing KeywordProduct links.
  - Cross-platform margin / profit dimensions are placeholders until P5
    provides Taobao/PDD match data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.services.selection.data_cleaning import PriceStats


_SENTINEL = object()


@dataclass
class ProductScoringInput:
    # Product self
    price: float = 0.0
    item_wants: int = 0
    title: str = ""

    # Derived from data_cleaning
    relevance_score: float = 0.0  # 0 ~ 10
    price_stats: PriceStats | None = None
    is_suspicious_low: bool = False

    # Cross-platform signals (filled by instant_search after P3-A gather).
    # `taobao_match_price` is the robust min price on taobao for the same
    # keyword; `pdd_min_price` is the robust min on PDD — combined with
    # `logistics_cost` and `loss_rate` below to estimate cost of goods.
    image_count: object = _SENTINEL
    seller_rating: object = _SENTINEL
    taobao_match_price: object = _SENTINEL
    pdd_min_price: object = _SENTINEL
    logistics_cost: float = 3.5
    loss_rate: float = 1.05
    estimated_cost: object = _SENTINEL  # legacy override; wins if explicitly set


@dataclass
class ProductDimension:
    name: str
    score: float
    max_score: float
    label: str
    has_data: bool = True


@dataclass
class ProductScoringResult:
    total_score: float
    decision: str
    dimensions: list[ProductDimension] = field(default_factory=list)
    risk_tags: list[str] = field(default_factory=list)
    scored_at: str = ""


# (name, max_score, neutral_score_if_no_data)
_DIMENSIONS: list[tuple[str, float, float]] = [
    ("价格竞争力",     15.0, 7.5),
    ("商品相关性",     10.0, 0.0),  # no-data case means title empty -> 0
    ("单品热度",       20.0, 10.0),
    ("单品跨平台价差", 15.0, 7.5),
    ("单品利润率",     15.0, 7.5),
    ("标题质量",       10.0, 5.0),
    ("客单价区间",     10.0, 5.0),
    ("图片/卖家信誉",   5.0, 2.5),
]


def _has_value(v: object) -> bool:
    return v is not _SENTINEL and v is not None


# ----------------------------------------------------------
# Dimension scoring
# ----------------------------------------------------------

def score_price_competitiveness_v2(
    price: float, median: float, is_suspicious_low: bool = False,
) -> tuple[float, str]:
    """Sweet-spot U curve vs the cleaned median.

    Why U-shape: loss-leader bait listings (ratio < 0.4) are almost always
    untradeable in practice, so they get a floor score rather than the old
    "lower is better" max. The sweet spot 0.6 ~ 0.8 is where resellers
    actually make money.
    """
    if is_suspicious_low:
        return 2.0, "可疑极低价"
    if median <= 0 or price <= 0:
        return 7.5, "无法比较"

    ratio = price / median
    if ratio < 0.4:
        return 2.0, "可疑极低价"
    if ratio < 0.6:
        return 8.0, "低得较多"
    if ratio < 0.8:
        return 15.0, "甜蜜区"
    if ratio < 1.0:
        return 12.0, "略低于主流"
    if ratio < 1.2:
        return 8.0, "接近主流价"
    if ratio < 1.5:
        return 4.0, "偏贵"
    return 1.0, "远高于主流"


def _score_relevance(relevance_score: float) -> tuple[float, str]:
    """relevance_score is already 0~10, so this is a near-identity mapping.

    Kept as a function for uniform UI labels.
    """
    r = max(0.0, min(10.0, relevance_score))
    if r >= 8.0:
        label = "高度相关"
    elif r >= 6.0:
        label = "相关"
    elif r >= 4.0:
        label = "部分相关"
    else:
        label = "相关性低"
    return round(r, 1), label


def _score_item_heat(item_wants: int) -> tuple[float, str]:
    """Single-item demand signal. last_sold_days will be folded in once
    the detail-page crawler ships."""
    if item_wants >= 50:
        return 20.0, "单品火爆"
    if item_wants >= 20:
        return 16.0, "单品需求好"
    if item_wants >= 10:
        return 12.0, "单品有需求"
    if item_wants >= 3:
        return 8.0, "有人想要"
    if item_wants >= 1:
        return 5.0, "少量想要"
    return 2.0, "无人问津"


def _score_title_quality(title: str) -> tuple[float, str]:
    if not title:
        return 1.0, "无标题"
    length = len(title)
    has_brand = bool(re.search(r"[A-Za-z]{2,}", title))
    has_condition = bool(
        re.search(r"(全新|99新|95新|9[5-9]成新|未拆|未使用)", title)
    )
    has_spec = bool(
        re.search(r"(\d+[gGmM][bB]|\d+[寸英]|\d+[wW]|套装|标准|畅拍)", title)
    )
    pts = 0
    if length >= 30:
        pts += 2
    elif length >= 15:
        pts += 1
    if has_brand:
        pts += 2
    if has_condition:
        pts += 2
    if has_spec:
        pts += 1
    if pts >= 6:
        return 10.0, "标题优质"
    if pts >= 4:
        return 7.0, "标题较好"
    if pts >= 2:
        return 4.0, "标题一般"
    return 2.0, "标题简陋"


def _score_price_zone(price: float) -> tuple[float, str]:
    """Absolute price zone for resale attractiveness."""
    if 50 <= price <= 300:
        return 10.0, "甜区"
    if (30 <= price < 50) or (300 < price <= 500):
        return 6.0, "可接受"
    if 500 < price <= 1000:
        return 4.0, "中高客单"
    if 20 <= price < 30:
        return 3.0, "偏低"
    return 1.0, "区间外"


def _score_cross_platform_gap_per_item(
    xianyu_price: float, taobao_price: float,
) -> tuple[float, str]:
    """Per-item gap = (xianyu - taobao) / taobao * 100%."""
    if taobao_price <= 0:
        return 7.5, "无淘宝价"
    gap = (xianyu_price - taobao_price) / taobao_price * 100.0
    if gap <= -40:
        return 15.0, "进货价骨折"
    if gap <= -20:
        return 12.0, "进货明显便宜"
    if gap <= 0:
        return 8.0, "进货略便宜"
    if gap <= 20:
        return 4.0, "进货差不多"
    return 1.0, "进货更贵"


def _score_profit_margin(margin: float) -> tuple[float, str]:
    """margin = (sell_price - cost) / sell_price * 100%."""
    if margin > 40:
        return 15.0, "高利润"
    if margin > 25:
        return 11.0, "利润较好"
    if margin > 15:
        return 7.0, "薄利"
    if margin > 5:
        return 3.0, "微利"
    return 1.0, "利润过低"


def _score_image_and_seller(
    image_count: object, seller_rating: object,
) -> tuple[float, str, bool]:
    """Placeholder — detail-page crawler needed."""
    has_any = _has_value(image_count) or _has_value(seller_rating)
    if not has_any:
        return 2.5, "待详情页数据", False

    score = 0.0
    bits: list[str] = []
    if _has_value(image_count):
        ic = int(image_count)  # type: ignore[arg-type]
        if ic >= 5:
            score += 3.0
            bits.append("图多")
        elif ic >= 3:
            score += 2.0
        elif ic >= 1:
            score += 1.0
        else:
            bits.append("无图")
    if _has_value(seller_rating):
        r = float(seller_rating)  # type: ignore[arg-type]
        if r >= 4.8:
            score += 2.0
            bits.append("卖家优")
        elif r >= 4.5:
            score += 1.5
        elif r >= 4.0:
            score += 1.0
        else:
            bits.append("卖家一般")
    return round(min(score, 5.0), 1), " ".join(bits) or "一般", True


def calculate_product_score(inp: ProductScoringInput) -> ProductScoringResult:
    """Compute the 8-dimension product-level score."""
    cfg = {name: (max_s, neutral) for name, max_s, neutral in _DIMENSIONS}
    dims: list[ProductDimension] = []
    total = 0.0
    risk_tags: list[str] = []

    def add(name: str, score: float, label: str, has_data: bool = True) -> None:
        nonlocal total
        max_s, _ = cfg[name]
        dims.append(ProductDimension(
            name=name, score=round(score, 1), max_score=max_s,
            label=label, has_data=has_data,
        ))
        total += score

    median = inp.price_stats.median if inp.price_stats else 0.0
    s, lbl = score_price_competitiveness_v2(
        inp.price, median, is_suspicious_low=inp.is_suspicious_low,
    )
    if lbl == "可疑极低价":
        risk_tags.append("suspicious_low")
    add("价格竞争力", s, lbl, has_data=(median > 0))

    s, lbl = _score_relevance(inp.relevance_score)
    if inp.relevance_score < 4.0:
        risk_tags.append("low_relevance")
    add("商品相关性", s, lbl)

    s, lbl = _score_item_heat(inp.item_wants)
    add("单品热度", s, lbl)

    if _has_value(inp.taobao_match_price):
        s, lbl = _score_cross_platform_gap_per_item(
            inp.price, float(inp.taobao_match_price),  # type: ignore[arg-type]
        )
        add("单品跨平台价差", s, lbl)
    else:
        _, neutral = cfg["单品跨平台价差"]
        add("单品跨平台价差", neutral, "待淘宝同款数据", has_data=False)

    # Prefer explicit estimated_cost, fall back to pdd_min_price * loss_rate + logistics.
    derived_cost: float | None = None
    if _has_value(inp.estimated_cost):
        derived_cost = float(inp.estimated_cost)  # type: ignore[arg-type]
    elif _has_value(inp.pdd_min_price):
        pdd = float(inp.pdd_min_price)  # type: ignore[arg-type]
        if pdd > 0:
            derived_cost = pdd * inp.loss_rate + inp.logistics_cost

    if derived_cost is not None and inp.price > 0:
        margin = (inp.price - derived_cost) / inp.price * 100.0
        s, lbl = _score_profit_margin(margin)
        add("单品利润率", s, lbl)
    else:
        _, neutral = cfg["单品利润率"]
        add("单品利润率", neutral, "待跨平台比价", has_data=False)

    s, lbl = _score_title_quality(inp.title)
    add("标题质量", s, lbl)

    s, lbl = _score_price_zone(inp.price)
    add("客单价区间", s, lbl)

    s, lbl, has = _score_image_and_seller(inp.image_count, inp.seller_rating)
    add("图片/卖家信誉", s, lbl, has_data=has)

    if total >= 75:
        decision = "buy"
    elif total >= 55:
        decision = "watch"
    else:
        decision = "skip"

    return ProductScoringResult(
        total_score=round(total, 1),
        decision=decision,
        dimensions=dims,
        risk_tags=risk_tags,
        scored_at=datetime.now(timezone.utc).isoformat(),
    )


PRODUCT_DECISION_LABELS = {
    "buy":   "推荐上架",
    "watch": "观察",
    "skip":  "跳过",
}
