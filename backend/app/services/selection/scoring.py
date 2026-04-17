"""
Xianyu multi-dimension scoring engine.

Dimensions split into two categories so that products within the same
keyword search can have meaningfully different scores:

  - Market-level (shared across all products in a keyword search):
    competitive landscape, overall price distribution, seller concentration.
  - Product-level (unique per product):
    price competitiveness vs peers, individual demand, title quality,
    absolute price zone.

Dimensions marked with has_data=False indicate that cross-platform
comparison data is not yet available; they receive a neutral midpoint
score instead of the minimum.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re


_SENTINEL = object()


@dataclass
class ScoringInput:
    # --- Market-level (same for all products in a search) ---
    active_listings: int = 0
    price_cv: float = 0.0
    total_wants: int = 0
    top1_seller_ratio: float = 0.0
    new_listing_ratio_7d: float = 0.0

    # --- Product-level ---
    unit_price: float = 0.0
    item_want_count: int = 0
    title: str = ""
    category_price_avg: float = 0.0
    category_price_min: float = 0.0
    category_price_max: float = 0.0

    # --- Cross-platform (optional, sentinel = not available) ---
    weekly_growth_rate: object = _SENTINEL
    profit_margin: object = _SENTINEL
    cross_platform_gap: object = _SENTINEL
    source_good_review_rate: object = _SENTINEL
    has_compat_complaints: bool = False


@dataclass
class DimensionResult:
    name: str
    raw_value: float
    score: float
    max_score: float
    weight: float
    label: str
    has_data: bool = True


@dataclass
class ScoringResult:
    total_score: float
    decision: str
    dimensions: list[DimensionResult] = field(default_factory=list)
    scored_at: str = ""


def _has_value(v: object) -> bool:
    return v is not _SENTINEL and v is not None


# ──────────────────────────────────────────
# Market-level dimensions
# ──────────────────────────────────────────

def score_active_listings(count: int) -> tuple[float, str]:
    if count <= 2:
        return 15, "蓝海"
    elif count <= 5:
        return 12, "较好"
    elif count <= 15:
        return 8, "中等"
    elif count <= 30:
        return 3, "压缩"
    else:
        return 0, "红海"


def score_price_cv(cv: float) -> tuple[float, str]:
    if cv > 30:
        return 10, "价格分散，机会大"
    elif cv > 15:
        return 7, "有一定空间"
    elif cv > 8:
        return 4, "价格较统一"
    else:
        return 2, "价格固化"


def score_seller_concentration(top1_ratio: float) -> tuple[float, str]:
    if top1_ratio < 20:
        return 8, "竞争分散"
    elif top1_ratio <= 40:
        return 5, "有头部卖家"
    else:
        return 2, "大卖家垄断"


def score_link_freshness(new_ratio_7d: float) -> tuple[float, str]:
    if new_ratio_7d < 30:
        return 5, "竞争稳定"
    elif new_ratio_7d <= 60:
        return 3, "竞争加剧中"
    else:
        return 1, "大量新入场"


# ──────────────────────────────────────────
# Product-level dimensions (differ per item)
# ──────────────────────────────────────────

def score_price_competitiveness(price: float, avg: float, pmin: float, pmax: float) -> tuple[float, str]:
    """How competitive is this product's price within its category?
    Lower price vs peers = better deal for resellers."""
    if avg <= 0 or pmax <= pmin:
        return 8, "无法比较"
    ratio = price / avg if avg > 0 else 1.0
    if ratio <= 0.5:
        return 15, "极低价，高性价比"
    elif ratio <= 0.7:
        return 13, "低于均价30%+"
    elif ratio <= 0.85:
        return 11, "低于均价"
    elif ratio <= 1.0:
        return 8, "接近均价"
    elif ratio <= 1.2:
        return 5, "高于均价"
    elif ratio <= 1.5:
        return 3, "偏贵"
    else:
        return 1, "远高于均价"


def score_want_heat(total_wants: int, item_wants: int) -> tuple[float, str]:
    """Demand signal: individual item wants weighted higher than keyword wants."""
    if item_wants >= 30:
        return 12, "单品需求火爆"
    elif item_wants >= 10:
        return 10, "单品需求好"
    elif item_wants >= 5:
        return 8, "单品有需求"
    elif item_wants >= 1:
        return 6, "有人想要"
    elif total_wants > 200:
        return 5, "关键词需求旺盛"
    elif total_wants > 50:
        return 4, "关键词有需求"
    elif total_wants > 10:
        return 3, "需求一般"
    else:
        return 2, "需求冷淡"


def score_title_quality(title: str) -> tuple[float, str]:
    """Listing title quality as a proxy for seller effort/professionalism."""
    if not title:
        return 1, "无标题"
    length = len(title)
    has_brand = bool(re.search(r'[A-Za-z]{2,}', title))
    has_condition = bool(re.search(r'(全新|99新|95新|9[5-9]成新|未拆|未使用)', title))
    has_spec = bool(re.search(r'(\d+[gGmM][bB]|\d+[寸英]|\d+[wW]|套装|标准|畅拍)', title))
    points = 0
    if length >= 30:
        points += 2
    elif length >= 15:
        points += 1
    if has_brand:
        points += 2
    if has_condition:
        points += 2
    if has_spec:
        points += 1
    if points >= 6:
        return 8, "标题优质"
    elif points >= 4:
        return 6, "标题较好"
    elif points >= 2:
        return 4, "标题一般"
    else:
        return 2, "标题简陋"


def score_price_zone(price: float) -> tuple[float, str]:
    """Is the absolute price in a good resale zone?"""
    if 50 <= price <= 300:
        return 5, "甜区"
    elif (30 <= price < 50) or (300 < price <= 500):
        return 3, "可接受"
    elif 500 < price <= 1000:
        return 2, "中高客单"
    else:
        return 1, "区间外"


# ──────────────────────────────────────────
# Cross-platform dimensions (may not have data yet)
# ──────────────────────────────────────────

def score_sales_trend(weekly_growth: float) -> tuple[float, str]:
    if weekly_growth > 20:
        return 10, "加速增长"
    elif weekly_growth > 5:
        return 7, "稳定增长"
    elif weekly_growth > 0:
        return 4, "增长放缓"
    else:
        return 1, "停滞/下降"


def score_profit_margin(margin: float) -> tuple[float, str]:
    if margin > 40:
        return 8, "高利润"
    elif margin > 25:
        return 6, "利润较好"
    elif margin > 15:
        return 4, "薄利"
    else:
        return 2, "利润过低"


def score_cross_platform_gap(gap: float) -> tuple[float, str]:
    if gap > 100:
        return 5, "信息差极大"
    elif gap > 50:
        return 4, "信息差较大"
    elif gap > 20:
        return 3, "有一定差价"
    else:
        return 1, "差价很小"


def score_review_quality(good_rate: float, has_compat: bool) -> tuple[float, str]:
    if good_rate > 95 and not has_compat:
        return 4, "评价优秀"
    elif good_rate > 90:
        return 3, "评价尚可"
    else:
        return 1, "评价较差"


DIMENSIONS_CONFIG = [
    ("价格竞争力", 15, 0.15),
    ("价格离散度CV", 10, 0.10),
    ("想要热度", 12, 0.12),
    ("标题质量", 8, 0.08),
    ("闲鱼在售链接数", 15, 0.15),
    ("卖家集中度", 8, 0.08),
    ("客单价区间", 5, 0.05),
    ("链接新鲜度", 5, 0.05),
    ("源平台销量趋势", 10, 0.10),
    ("跨平台价差比", 5, 0.05),
    ("利润率", 8, 0.08),
]

_NEUTRAL_SCORES = {
    "源平台销量趋势": (5, "待跨平台比价"),
    "跨平台价差比": (3, "待跨平台比价"),
    "利润率": (4, "待跨平台比价"),
}


def calculate_xianyu_score(input_data: ScoringInput) -> ScoringResult:
    """Calculate the multi-dimension score for a product on Xianyu.

    Combines market-level and product-level dimensions so that products
    within the same search keyword can have meaningfully different scores.
    """
    has_growth = _has_value(input_data.weekly_growth_rate)
    has_margin = _has_value(input_data.profit_margin)
    has_gap = _has_value(input_data.cross_platform_gap)

    dim_specs: list[tuple] = [
        ("价格竞争力",
         lambda: score_price_competitiveness(
             input_data.unit_price,
             input_data.category_price_avg,
             input_data.category_price_min,
             input_data.category_price_max),
         True),
        ("价格离散度CV",
         lambda: score_price_cv(input_data.price_cv),
         True),
        ("想要热度",
         lambda: score_want_heat(input_data.total_wants, input_data.item_want_count),
         True),
        ("标题质量",
         lambda: score_title_quality(input_data.title),
         True),
        ("闲鱼在售链接数",
         lambda: score_active_listings(input_data.active_listings),
         True),
        ("卖家集中度",
         lambda: score_seller_concentration(input_data.top1_seller_ratio),
         True),
        ("客单价区间",
         lambda: score_price_zone(input_data.unit_price),
         True),
        ("链接新鲜度",
         lambda: score_link_freshness(input_data.new_listing_ratio_7d),
         True),
        ("源平台销量趋势",
         (lambda: score_sales_trend(input_data.weekly_growth_rate)) if has_growth else None,
         has_growth),
        ("跨平台价差比",
         (lambda: score_cross_platform_gap(input_data.cross_platform_gap)) if has_gap else None,
         has_gap),
        ("利润率",
         (lambda: score_profit_margin(input_data.profit_margin)) if has_margin else None,
         has_margin),
    ]

    config_map = {name: (max_s, weight) for name, max_s, weight in DIMENSIONS_CONFIG}
    dimensions = []
    total = 0.0

    for name, func, data_available in dim_specs:
        max_score, weight = config_map[name]
        if data_available and func is not None:
            score, label = func()
            has_data = True
        else:
            score, label = _NEUTRAL_SCORES.get(name, (max_score // 2, "待数据"))
            has_data = False
        dimensions.append(DimensionResult(
            name=name,
            raw_value=score,
            score=score,
            max_score=max_score,
            weight=weight,
            label=label,
            has_data=has_data,
        ))
        total += score

    if total >= 80:
        decision = "strong_recommend"
    elif total >= 60:
        decision = "worth_try"
    elif total >= 40:
        decision = "average"
    else:
        decision = "skip"

    return ScoringResult(
        total_score=round(total, 1),
        decision=decision,
        dimensions=dimensions,
        scored_at=datetime.now(timezone.utc).isoformat(),
    )


DECISION_LABELS = {
    "strong_recommend": "强烈推荐",
    "worth_try": "值得尝试",
    "average": "一般",
    "skip": "跳过",
}
