"""
XHS 5-dimension scoring engine.
Evaluates product suitability for Xiaohongshu content commerce.
Based on Appendix B scoring rules.
"""
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class XhsScoringInput:
    topic_view_count: int = 0
    note_growth_30d_pct: float = 0.0
    photogenic_level: int = 3             # 1-4 (1=not photogenic, 4=very photogenic)
    content_form_count: int = 1           # how many content forms possible
    source_review_photo_quality: int = 2  # 1-3 (1=poor, 3=rich)
    category_interaction_rate: float = 0.0  # %
    purchase_intent_ratio: float = 0.0    # %
    new_notes_30d: int = 0
    sales_notes_30d: int = 0
    profit_margin_pct: float = 0.0        # %


@dataclass
class XhsDimensionResult:
    name: str
    raw_value: float
    score: float
    max_score: float
    label: str


@dataclass
class XhsScoringResult:
    total_score: float
    decision: str
    dimensions: list[XhsDimensionResult]
    scored_at: str


def score_topic_heat(view_count: int, growth_pct: float) -> tuple[float, str]:
    """Topic heat: max 25 points."""
    if view_count > 100_000_000 and growth_pct > 15:
        return 25, "热门赛道，高速增长"
    elif view_count > 50_000_000 and growth_pct > 5:
        return 18, "热门赛道"
    elif view_count > 10_000_000 and growth_pct > 0:
        return 12, "中等热度"
    else:
        return 5, "话题冷门"


def score_photogenic(level: int, form_count: int, photo_quality: int) -> tuple[float, str]:
    """Content photogenic score: max 20 points."""
    if level >= 4 and form_count >= 3 and photo_quality >= 3:
        return 20, "极其上镜，多种内容"
    elif level >= 3 and form_count >= 2:
        return 14, "上镜，2种以上内容"
    elif level >= 2:
        return 8, "需要场景布置"
    else:
        return 3, "不上镜"


def score_seed_conversion(interaction_rate: float, intent_ratio: float) -> tuple[float, str]:
    """Seed conversion power: max 20 points."""
    if interaction_rate > 4 and intent_ratio > 30:
        return 20, "转化力极强"
    elif interaction_rate > 2.5 and intent_ratio > 15:
        return 14, "转化力较好"
    elif interaction_rate > 1:
        return 8, "转化力一般"
    else:
        return 3, "转化力弱"


def score_competitor_density(new_notes_30d: int, sales_notes_30d: int) -> tuple[float, str]:
    """Competitor note density: max 20 points."""
    if new_notes_30d < 50 and sales_notes_30d < 10:
        return 20, "蓝海，竞争极小"
    elif new_notes_30d < 200 and sales_notes_30d < 50:
        return 14, "中等竞争"
    elif new_notes_30d < 500:
        return 8, "竞争较大"
    else:
        return 3, "红海"


def score_profit_margin(margin_pct: float) -> tuple[float, str]:
    """Profit margin: max 15 points (XHS supports higher content premium)."""
    if margin_pct > 60:
        return 15, "高溢价空间"
    elif margin_pct > 40:
        return 11, "利润较好"
    elif margin_pct > 20:
        return 7, "利润一般"
    else:
        return 3, "利润过低"


DIMENSIONS_CONFIG = [
    ("话题热度", 25),
    ("内容可拍性", 20),
    ("种草转化力", 20),
    ("竞品笔记密度", 20),
    ("利润空间", 15),
]


def calculate_xhs_score(input_data: XhsScoringInput) -> XhsScoringResult:
    """Calculate the 5-dimension XHS selection score."""
    scoring_funcs = [
        lambda: score_topic_heat(input_data.topic_view_count, input_data.note_growth_30d_pct),
        lambda: score_photogenic(input_data.photogenic_level, input_data.content_form_count, input_data.source_review_photo_quality),
        lambda: score_seed_conversion(input_data.category_interaction_rate, input_data.purchase_intent_ratio),
        lambda: score_competitor_density(input_data.new_notes_30d, input_data.sales_notes_30d),
        lambda: score_profit_margin(input_data.profit_margin_pct),
    ]

    dimensions = []
    total = 0.0

    for i, func in enumerate(scoring_funcs):
        name, max_score = DIMENSIONS_CONFIG[i]
        score, label = func()
        dimensions.append(XhsDimensionResult(
            name=name,
            raw_value=score,
            score=score,
            max_score=max_score,
            label=label,
        ))
        total += score

    if total >= 80:
        decision = "strong_recommend"
    elif total >= 60:
        decision = "worth_doing"
    elif total >= 40:
        decision = "wait_and_see"
    else:
        decision = "not_suitable"

    return XhsScoringResult(
        total_score=round(total, 1),
        decision=decision,
        dimensions=dimensions,
        scored_at=datetime.now(timezone.utc).isoformat(),
    )


XHS_DECISION_LABELS = {
    "strong_recommend": "强烈推荐，优先出内容",
    "worth_doing": "值得做，安排内容排期",
    "wait_and_see": "观望，话题起来再做",
    "not_suitable": "不适合小红书",
}
