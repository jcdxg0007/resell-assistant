"""
Xianyu 10-dimension scoring engine.
Evaluates products based on market data, competition, and profit potential.
"""
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ScoringInput:
    active_listings: int = 0
    price_cv: float = 0.0
    total_wants: int = 0
    weekly_growth_rate: float = 0.0       # source platform sales trend %
    top1_seller_ratio: float = 0.0        # TOP1 seller share
    profit_margin: float = 0.0            # (sale_price - cost) / cost
    cross_platform_gap: float = 0.0       # (xianyu_avg - source) / source * 100
    new_listing_ratio_7d: float = 0.0     # 7-day new listings / total
    source_good_review_rate: float = 0.0  # source platform review rate %
    has_compat_complaints: bool = False
    unit_price: float = 0.0               # sale price in CNY


@dataclass
class DimensionResult:
    name: str
    raw_value: float
    score: float
    max_score: float
    weight: float
    label: str


@dataclass
class ScoringResult:
    total_score: float
    decision: str
    dimensions: list[DimensionResult]
    scored_at: str


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
        return 15, "价格分散，机会大"
    elif cv > 15:
        return 11, "有一定空间"
    elif cv > 8:
        return 6, "价格较统一"
    else:
        return 2, "价格固化"


def score_want_heat(total_wants: int) -> tuple[float, str]:
    if total_wants > 200:
        return 12, "需求旺盛"
    elif total_wants > 50:
        return 9, "需求较好"
    elif total_wants > 10:
        return 5, "需求一般"
    else:
        return 2, "需求冷淡"


def score_sales_trend(weekly_growth: float) -> tuple[float, str]:
    if weekly_growth > 20:
        return 12, "加速增长"
    elif weekly_growth > 5:
        return 9, "稳定增长"
    elif weekly_growth > 0:
        return 5, "增长放缓"
    else:
        return 1, "停滞/下降"


def score_seller_concentration(top1_ratio: float) -> tuple[float, str]:
    if top1_ratio < 20:
        return 10, "竞争分散"
    elif top1_ratio <= 40:
        return 6, "有头部卖家"
    else:
        return 2, "大卖家垄断"


def score_profit_margin(margin: float) -> tuple[float, str]:
    if margin > 40:
        return 10, "高利润"
    elif margin > 25:
        return 8, "利润较好"
    elif margin > 15:
        return 5, "薄利"
    else:
        return 2, "利润过低"


def score_cross_platform_gap(gap: float) -> tuple[float, str]:
    if gap > 100:
        return 8, "信息差极大"
    elif gap > 50:
        return 6, "信息差较大"
    elif gap > 20:
        return 4, "有一定差价"
    else:
        return 1, "差价很小"


def score_link_freshness(new_ratio_7d: float) -> tuple[float, str]:
    if new_ratio_7d < 30:
        return 8, "竞争稳定"
    elif new_ratio_7d <= 60:
        return 5, "竞争加剧中"
    else:
        return 2, "大量新入场"


def score_review_quality(good_rate: float, has_compat: bool) -> tuple[float, str]:
    if good_rate > 95 and not has_compat:
        return 5, "评价优秀"
    elif good_rate > 90:
        return 3, "评价尚可"
    else:
        return 1, "评价较差"


def score_unit_price(price: float) -> tuple[float, str]:
    if 50 <= price <= 300:
        return 5, "甜区"
    elif (30 <= price < 50) or (300 < price <= 500):
        return 3, "可接受"
    else:
        return 1, "区间外"


DIMENSIONS_CONFIG = [
    ("闲鱼在售链接数", 15, 0.15),
    ("价格离散度CV", 15, 0.15),
    ("想要热度", 12, 0.12),
    ("源平台销量趋势", 12, 0.12),
    ("卖家集中度", 10, 0.10),
    ("利润率", 10, 0.10),
    ("跨平台价差比", 8, 0.08),
    ("链接新鲜度", 8, 0.08),
    ("源平台评价质量", 5, 0.05),
    ("客单价区间", 5, 0.05),
]


def calculate_xianyu_score(input_data: ScoringInput) -> ScoringResult:
    """Calculate the 10-dimension score for a product on Xianyu."""
    scoring_funcs = [
        lambda: score_active_listings(input_data.active_listings),
        lambda: score_price_cv(input_data.price_cv),
        lambda: score_want_heat(input_data.total_wants),
        lambda: score_sales_trend(input_data.weekly_growth_rate),
        lambda: score_seller_concentration(input_data.top1_seller_ratio),
        lambda: score_profit_margin(input_data.profit_margin),
        lambda: score_cross_platform_gap(input_data.cross_platform_gap),
        lambda: score_link_freshness(input_data.new_listing_ratio_7d),
        lambda: score_review_quality(input_data.source_good_review_rate, input_data.has_compat_complaints),
        lambda: score_unit_price(input_data.unit_price),
    ]

    dimensions = []
    total = 0.0

    for i, func in enumerate(scoring_funcs):
        name, max_score, weight = DIMENSIONS_CONFIG[i]
        score, label = func()
        dimensions.append(DimensionResult(
            name=name,
            raw_value=score,
            score=score,
            max_score=max_score,
            weight=weight,
            label=label,
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
