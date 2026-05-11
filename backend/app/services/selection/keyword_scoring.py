"""
Keyword-level (market) scoring - 100-point scale, 8 dimensions.

Answers the question "is this keyword a good niche to sell into?"
Separate from product_scoring.py which grades individual listings.

Dimensions with no data yet receive the neutral midpoint of their weight
(so the score is not artificially depressed while P5 crawlers are being
built). has_data=False flags them for the UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


_SENTINEL = object()


@dataclass
class KeywordScoringInput:
    # Xianyu-sourced (all currently available)
    active_listings: int = 0
    price_cv: float = 0.0
    top1_seller_ratio: float = 0.0
    new_listing_ratio_7d: float = 0.0
    total_wants: int = 0

    # Cross-platform placeholders (sentinel = not available yet)
    taobao_search_volume: object = _SENTINEL
    weekly_growth_rate: object = _SENTINEL
    xhs_hotness: object = _SENTINEL
    cross_platform_gap_avg: object = _SENTINEL


@dataclass
class KeywordDimension:
    name: str
    score: float
    max_score: float
    label: str
    has_data: bool = True


@dataclass
class KeywordScoringResult:
    total_score: float
    decision: str
    dimensions: list[KeywordDimension] = field(default_factory=list)
    scored_at: str = ""


# (name, max_score, neutral_score_if_no_data)
_DIMENSIONS: list[tuple[str, float, float]] = [
    ("闲鱼在售链接数", 20.0, 10.0),
    ("价格离散度CV",   15.0, 7.5),
    ("卖家集中度",     10.0, 5.0),
    ("链接新鲜度",     10.0, 5.0),
    ("赛道需求",       15.0, 7.5),
    ("源平台销量趋势", 15.0, 7.5),
    ("小红书种草热度", 10.0, 5.0),
    ("赛道信息差",      5.0, 2.5),
]


def _has_value(v: object) -> bool:
    return v is not _SENTINEL and v is not None


def _score_active_listings(count: int) -> tuple[float, str]:
    if count <= 2:
        return 20.0, "蓝海"
    if count <= 5:
        return 16.0, "较好"
    if count <= 15:
        return 11.0, "中等"
    if count <= 30:
        return 5.0, "压缩"
    return 1.0, "红海"


def _score_price_cv(cv: float) -> tuple[float, str]:
    if cv > 30:
        return 15.0, "价格分散，机会大"
    if cv > 15:
        return 11.0, "有一定空间"
    if cv > 8:
        return 6.0, "价格较统一"
    return 3.0, "价格固化"


def _score_seller_concentration(top1_ratio: float) -> tuple[float, str]:
    if top1_ratio < 20:
        return 10.0, "竞争分散"
    if top1_ratio <= 40:
        return 6.0, "有头部卖家"
    return 2.0, "大卖家垄断"


def _score_link_freshness(new_ratio_7d: float) -> tuple[float, str]:
    if new_ratio_7d < 30:
        return 10.0, "竞争稳定"
    if new_ratio_7d <= 60:
        return 6.0, "竞争加剧中"
    return 2.0, "大量新入场"


def _score_niche_demand(
    total_wants: int,
    taobao_volume: object,
) -> tuple[float, str, bool]:
    """Combine Xianyu wants and Taobao search volume.

    When Taobao data is unavailable, scale the Xianyu-only signal up (to
    roughly 80% of max) so the keyword isn't punished purely for the data
    gap. ``has_data`` flags this partial state.
    """
    has_taobao = _has_value(taobao_volume)

    if total_wants > 500:
        xy, xy_label = 8.0, "想要旺盛"
    elif total_wants > 200:
        xy, xy_label = 6.0, "想要良好"
    elif total_wants > 50:
        xy, xy_label = 4.0, "有需求"
    elif total_wants > 10:
        xy, xy_label = 2.0, "需求一般"
    else:
        xy, xy_label = 1.0, "冷淡"

    if not has_taobao:
        return round(xy * 1.5, 1), xy_label + "（缺淘宝数据）", False

    tb = float(taobao_volume)  # type: ignore[arg-type]
    if tb > 10000:
        tb_part = 7.0
    elif tb > 3000:
        tb_part = 5.0
    elif tb > 500:
        tb_part = 3.0
    elif tb > 50:
        tb_part = 1.5
    else:
        tb_part = 0.5
    return round(xy + tb_part, 1), f"{xy_label}+淘系", True


def _score_sales_trend(weekly_growth: float) -> tuple[float, str]:
    if weekly_growth > 20:
        return 15.0, "加速增长"
    if weekly_growth > 5:
        return 11.0, "稳定增长"
    if weekly_growth > 0:
        return 6.0, "增长放缓"
    return 2.0, "停滞/下降"


def _score_xhs_hotness(hotness: float) -> tuple[float, str]:
    if hotness > 80:
        return 10.0, "种草爆发"
    if hotness > 50:
        return 7.0, "种草活跃"
    if hotness > 20:
        return 4.0, "有讨论"
    return 1.0, "冷清"


def _score_cross_platform_gap(gap: float) -> tuple[float, str]:
    if gap > 100:
        return 5.0, "信息差极大"
    if gap > 50:
        return 4.0, "信息差较大"
    if gap > 20:
        return 3.0, "有一定差价"
    return 1.0, "差价很小"


def calculate_keyword_score(inp: KeywordScoringInput) -> KeywordScoringResult:
    """Compute the 8-dimension keyword-level score."""
    cfg = {name: (max_s, neutral) for name, max_s, neutral in _DIMENSIONS}
    dims: list[KeywordDimension] = []
    total = 0.0

    def add(name: str, score: float, label: str, has_data: bool = True) -> None:
        nonlocal total
        max_s, _ = cfg[name]
        dims.append(KeywordDimension(
            name=name, score=round(score, 1), max_score=max_s,
            label=label, has_data=has_data,
        ))
        total += score

    s, lbl = _score_active_listings(inp.active_listings)
    add("闲鱼在售链接数", s, lbl)

    s, lbl = _score_price_cv(inp.price_cv)
    add("价格离散度CV", s, lbl)

    s, lbl = _score_seller_concentration(inp.top1_seller_ratio)
    add("卖家集中度", s, lbl)

    s, lbl = _score_link_freshness(inp.new_listing_ratio_7d)
    add("链接新鲜度", s, lbl)

    s, lbl, has = _score_niche_demand(inp.total_wants, inp.taobao_search_volume)
    add("赛道需求", s, lbl, has_data=has)

    if _has_value(inp.weekly_growth_rate):
        s, lbl = _score_sales_trend(float(inp.weekly_growth_rate))  # type: ignore[arg-type]
        add("源平台销量趋势", s, lbl)
    else:
        _, neutral = cfg["源平台销量趋势"]
        add("源平台销量趋势", neutral, "待跨平台比价", has_data=False)

    if _has_value(inp.xhs_hotness):
        s, lbl = _score_xhs_hotness(float(inp.xhs_hotness))  # type: ignore[arg-type]
        add("小红书种草热度", s, lbl)
    else:
        _, neutral = cfg["小红书种草热度"]
        add("小红书种草热度", neutral, "待小红书数据", has_data=False)

    if _has_value(inp.cross_platform_gap_avg):
        s, lbl = _score_cross_platform_gap(float(inp.cross_platform_gap_avg))  # type: ignore[arg-type]
        add("赛道信息差", s, lbl)
    else:
        _, neutral = cfg["赛道信息差"]
        add("赛道信息差", neutral, "待跨平台比价", has_data=False)

    if total >= 75:
        decision = "hot"
    elif total >= 55:
        decision = "worth_try"
    elif total >= 35:
        decision = "saturated"
    else:
        decision = "skip"

    return KeywordScoringResult(
        total_score=round(total, 1),
        decision=decision,
        dimensions=dims,
        scored_at=datetime.now(timezone.utc).isoformat(),
    )


KEYWORD_DECISION_LABELS = {
    "hot":       "赛道优质",
    "worth_try": "值得尝试",
    "saturated": "已饱和",
    "skip":      "跳过",
}
