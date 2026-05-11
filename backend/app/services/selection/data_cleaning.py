"""
Data cleaning for keyword-level product samples.

Responsibilities:
  1. Core-token extraction via jieba (shared by relevance + suite detection).
  2. Per-product relevance score (0~10) — Jaccard-like hit ratio of the
     keyword's core tokens against the listing title.
  3. Suite / single-item grouping, because a 50-yuan single accessory and a
     500-yuan bundle should not share the same price baseline.
  4. Robust price statistics (median / P25 / P75) with two cleaning passes:
     - absolute floor:  ``max(10, median * 0.2)`` (or ``taobao_min * 0.3``
       when a cross-platform anchor is available)
     - IQR trim:        drop samples outside ``[Q1 - 1.5·IQR, Q3 + 1.5·IQR]``
  5. Per-product risk tags so the UI can warn the operator before they buy.

All outputs are plain dataclasses; no DB or network access here.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

import jieba


# Stop-words pulled from first-pass tokenization of typical Xianyu queries;
# short enough that a list is fine.
_STOP_WORDS: frozenset[str] = frozenset(
    {"的", "和", "与", "或", "及", "等", "有", "是", "了", "在",
     " ", "", "/", "-", "_"}
)

SUITE_PATTERN = re.compile(
    r"(套装|全套|全家桶|大礼包|组合装|组合|搭配套|搭配|一套)"
)


# ──────────────────────────────────────────
# Dataclasses
# ──────────────────────────────────────────

@dataclass
class PriceStats:
    """Robust price distribution of a cleaned sample.

    ``sample_size`` is the post-clean count, not the raw input size.
    ``suspicious_count`` is the number of samples dropped at the floor stage;
    IQR-trimmed samples are not counted as suspicious (they may simply be
    rare but legitimate).
    """
    median: float
    p25: float
    p75: float
    sample_size: int
    suspicious_count: int


@dataclass
class CleanedProduct:
    product_id: str
    title: str
    price: float
    item_wants: int
    relevance_score: float        # 0 ~ 10
    is_suite: bool
    is_suspicious_low: bool
    risk_tags: list[str] = field(default_factory=list)


# Empty stats that callers can feed into scoring when the sample is so small
# that median/P25/P75 are meaningless — avoids branching downstream.
_EMPTY_STATS = PriceStats(
    median=0.0, p25=0.0, p75=0.0, sample_size=0, suspicious_count=0,
)


# ──────────────────────────────────────────
# Tokenization & relevance
# ──────────────────────────────────────────

def tokenize_keyword(keyword: str) -> list[str]:
    """Extract the core tokens from a search keyword.

    Uses ``cut_for_search`` which emits overlapping fragments (better recall
    for short Chinese-English mixed strings like 'action4 运动相机'). Keeps:
      - tokens with length >= 2
      - ASCII tokens with length >= 2 (eg. 'action4', 'dji')
      - Chinese tokens of any length >= 2
    """
    kw = keyword.strip()
    if not kw:
        return []
    raw = list(jieba.cut_for_search(kw))
    out: list[str] = []
    seen: set[str] = set()
    for t in raw:
        t = t.strip().lower()
        if not t or t in _STOP_WORDS:
            continue
        if len(t) < 2:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    # Fall-back: if nothing survived (e.g. a 1-char keyword), keep the raw
    # keyword so we still have something to match against.
    if not out:
        out.append(kw.lower())
    return out


def calculate_relevance(title: str, core_tokens: list[str]) -> float:
    """Return a 0~10 score for how well ``title`` matches the core tokens.

    Algorithm: fraction of core tokens found as substrings of the title,
    scaled to 0~10. Substring match (not token match) lets us handle
    titles that glue words together like 'action4运动相机自拍杆'.
    """
    if not core_tokens:
        return 10.0  # nothing to check → pass-through
    if not title:
        return 0.0
    t_lower = title.lower()
    hits = sum(1 for tok in core_tokens if tok in t_lower)
    return round(10.0 * hits / len(core_tokens), 2)


def detect_suite(title: str) -> bool:
    """True if the title signals a bundle/package listing."""
    if not title:
        return False
    return bool(SUITE_PATTERN.search(title))


# ──────────────────────────────────────────
# Price-distribution helpers
# ──────────────────────────────────────────

def _percentile(sorted_prices: list[float], pct: float) -> float:
    """Linear-interpolation percentile; ``sorted_prices`` must be sorted.

    Equivalent to numpy's default percentile but avoids the numpy dep.
    """
    if not sorted_prices:
        return 0.0
    if len(sorted_prices) == 1:
        return sorted_prices[0]
    k = (len(sorted_prices) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_prices) - 1)
    if f == c:
        return sorted_prices[f]
    return sorted_prices[f] + (sorted_prices[c] - sorted_prices[f]) * (k - f)


def _compute_stats(
    prices: list[float],
    *,
    taobao_min_price: float | None = None,
) -> PriceStats:
    """Two-pass cleaning:
      1. absolute floor drops obvious bait-and-switch listings (e.g. 9.9 yuan
         loss leaders). ``suspicious_count`` records how many were dropped.
      2. IQR-trim removes statistical outliers so the reported median/quartiles
         reflect the main body of the distribution.
    """
    if not prices:
        return _EMPTY_STATS

    cleaned = sorted(p for p in prices if p and p > 0)
    if not cleaned:
        return _EMPTY_STATS

    provisional_median = statistics.median(cleaned)
    if taobao_min_price and taobao_min_price > 0:
        floor = taobao_min_price * 0.3
    else:
        floor = max(10.0, provisional_median * 0.2)

    above_floor = [p for p in cleaned if p >= floor]
    suspicious_count = len(cleaned) - len(above_floor)

    if len(above_floor) < 2:
        # Sample too small for IQR; use what we have.
        return PriceStats(
            median=statistics.median(above_floor) if above_floor else 0.0,
            p25=above_floor[0] if above_floor else 0.0,
            p75=above_floor[-1] if above_floor else 0.0,
            sample_size=len(above_floor),
            suspicious_count=suspicious_count,
        )

    q1 = _percentile(above_floor, 0.25)
    q3 = _percentile(above_floor, 0.75)
    iqr = q3 - q1
    lo = q1 - 1.5 * iqr
    hi = q3 + 1.5 * iqr
    trimmed = [p for p in above_floor if lo <= p <= hi]
    if len(trimmed) < 2:
        trimmed = above_floor

    trimmed.sort()
    return PriceStats(
        median=statistics.median(trimmed),
        p25=_percentile(trimmed, 0.25),
        p75=_percentile(trimmed, 0.75),
        sample_size=len(trimmed),
        suspicious_count=suspicious_count,
    )


# ──────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────

def clean_keyword_sample(
    raw_items: list[dict],
    keyword: str,
    *,
    taobao_min_price: float | None = None,
) -> tuple[list[CleanedProduct], PriceStats, PriceStats]:
    """Clean a batch of products crawled under one keyword.

    ``raw_items`` must carry at minimum ``product_id``, ``title``, ``price``,
    and optionally ``item_wants`` / ``want_count``.

    Returns:
      cleaned_items:   one CleanedProduct per input (nothing is dropped here;
                       downstream may hard-filter on ``relevance_score``).
      single_stats:    PriceStats for the single-item group.
      suite_stats:     PriceStats for the suite/bundle group. If the keyword
                       has no bundle listings, this is an empty stats object.
    """
    core_tokens = tokenize_keyword(keyword)

    cleaned: list[CleanedProduct] = []
    single_prices: list[float] = []
    suite_prices: list[float] = []

    for item in raw_items:
        pid = item.get("product_id") or item.get("item_id") or ""
        if not pid:
            continue
        title = item.get("title") or ""
        price = float(item.get("price") or 0)
        item_wants = int(item.get("item_wants") or item.get("want_count") or 0)

        relevance = calculate_relevance(title, core_tokens)
        is_suite = detect_suite(title)

        tags: list[str] = []
        if relevance < 4.0:
            tags.append("low_relevance")
        if is_suite:
            tags.append("suite")

        cleaned.append(CleanedProduct(
            product_id=str(pid),
            title=title,
            price=price,
            item_wants=item_wants,
            relevance_score=relevance,
            is_suite=is_suite,
            is_suspicious_low=False,  # filled in below after stats are known
            risk_tags=tags,
        ))
        if price > 0 and relevance >= 4.0:
            # Irrelevant listings also distort the baseline; exclude them
            # from the statistics pool but still return them to the caller.
            (suite_prices if is_suite else single_prices).append(price)

    single_stats = _compute_stats(single_prices, taobao_min_price=taobao_min_price)
    suite_stats = _compute_stats(suite_prices, taobao_min_price=taobao_min_price)

    # Second pass: mark each product against its group's floor so the UI can
    # surface the warning and scoring can penalize the price dimension.
    for cp in cleaned:
        stats = suite_stats if cp.is_suite else single_stats
        floor = (
            (taobao_min_price * 0.3)
            if taobao_min_price and taobao_min_price > 0
            else max(10.0, stats.median * 0.2 if stats.median > 0 else 10.0)
        )
        if cp.price > 0 and cp.price < floor:
            cp.is_suspicious_low = True
            if "suspicious_low" not in cp.risk_tags:
                cp.risk_tags.append("suspicious_low")

    return cleaned, single_stats, suite_stats
