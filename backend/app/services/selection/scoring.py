"""
Backwards-compatible re-exports.

The real scoring logic lives in :mod:`data_cleaning`, :mod:`keyword_scoring`,
and :mod:`product_scoring` since the P2 refactor. This module exists so
external callers (imports from API layer, tests, etc.) keep working.

New code should import directly from the specialized modules.
"""
from app.services.selection.data_cleaning import (
    CleanedProduct,
    PriceStats,
    calculate_relevance,
    clean_keyword_sample,
    detect_suite,
    tokenize_keyword,
)
from app.services.selection.keyword_scoring import (
    KEYWORD_DECISION_LABELS,
    KeywordDimension,
    KeywordScoringInput,
    KeywordScoringResult,
    calculate_keyword_score,
)
from app.services.selection.product_scoring import (
    PRODUCT_DECISION_LABELS,
    ProductDimension,
    ProductScoringInput,
    ProductScoringResult,
    calculate_product_score,
    score_price_competitiveness_v2,
)

# Unified decision-label table covering both keyword and product decisions
# plus the legacy {"strong_recommend", "worth_try", ...} values so old rows
# written before P2 still render something sensible in the UI.
DECISION_LABELS: dict[str, str] = {
    **PRODUCT_DECISION_LABELS,
    **KEYWORD_DECISION_LABELS,
    "strong_recommend": "强烈推荐 (legacy)",
    "worth_try":        "值得尝试 (legacy)",
    "average":          "一般 (legacy)",
    "skip":             "跳过",
}


__all__ = [
    # data cleaning
    "CleanedProduct",
    "PriceStats",
    "calculate_relevance",
    "clean_keyword_sample",
    "detect_suite",
    "tokenize_keyword",
    # keyword scoring
    "KeywordDimension",
    "KeywordScoringInput",
    "KeywordScoringResult",
    "calculate_keyword_score",
    "KEYWORD_DECISION_LABELS",
    # product scoring
    "ProductDimension",
    "ProductScoringInput",
    "ProductScoringResult",
    "calculate_product_score",
    "score_price_competitiveness_v2",
    "PRODUCT_DECISION_LABELS",
    # combined label map
    "DECISION_LABELS",
]
