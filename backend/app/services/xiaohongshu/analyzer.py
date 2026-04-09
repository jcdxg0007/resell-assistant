"""
XHS hot article & content analysis engine.
Analyzes note data for content strategy insights.
"""
import re
from collections import Counter
from datetime import datetime

from loguru import logger


PURCHASE_INTENT_KEYWORDS = [
    "怎么买", "链接", "多少钱", "已入手", "求推荐", "哪里买",
    "求分享", "已收藏", "已下单", "想买", "有链接", "在哪买",
    "求链接", "能发我", "私我", "多少钱",
]

POSITIVE_FEEDBACK_KEYWORDS = [
    "好用", "推荐", "回购", "已入手", "绝了", "太好了",
    "必入", "闭眼买", "绝绝子", "yyds", "安利",
]

SEED_TITLE_KEYWORDS = [
    "必入", "绝绝子", "闭眼买", "yyds", "天花板", "良心推荐",
    "强推", "后悔没早买", "人手一个", "安利", "真的好用",
    "宝藏", "小众", "平价", "白菜价", "性价比",
]


def analyze_title_keywords(notes: list[dict]) -> dict:
    """
    Analyze title keywords across notes.
    Returns high-frequency seed keywords and their counts.
    """
    word_counter = Counter()

    for note in notes:
        title = note.get("title", "")
        for kw in SEED_TITLE_KEYWORDS:
            if kw in title:
                word_counter[kw] += 1

        # Also extract short segments from titles
        segments = re.findall(r'[\u4e00-\u9fff]{2,6}', title)
        for seg in segments:
            word_counter[seg] += 1

    # Filter to meaningful frequency
    total = len(notes) or 1
    high_freq = {
        word: {"count": count, "ratio": round(count / total * 100, 1)}
        for word, count in word_counter.most_common(30)
        if count >= 2
    }

    return {
        "total_notes_analyzed": len(notes),
        "top_keywords": high_freq,
        "seed_words_found": [w for w in SEED_TITLE_KEYWORDS if word_counter.get(w, 0) >= 2],
    }


def analyze_content_structure(notes: list[dict]) -> dict:
    """
    Classify note content structures.
    Identifies dominant note types in the category.
    """
    types = Counter()
    body_lengths = []

    for note in notes:
        body = note.get("body", "")
        body_lengths.append(len(body))

        if any(kw in body for kw in ["测评", "对比", "vs", "VS", "pk", "PK"]):
            types["测评对比型"] += 1
        elif any(kw in body for kw in ["教程", "步骤", "怎么", "如何", "第一步"]):
            types["教程型"] += 1
        elif any(kw in body for kw in ["合集", "推荐", "盘点", "清单", "必买"]):
            types["合集种草型"] += 1
        elif any(kw in body for kw in ["开箱", "拆箱", "到手"]):
            types["开箱型"] += 1
        else:
            types["场景分享型"] += 1

    avg_length = sum(body_lengths) / len(body_lengths) if body_lengths else 0

    return {
        "structure_distribution": dict(types.most_common()),
        "avg_body_length": round(avg_length),
        "recommended_length": "300-800字" if avg_length > 300 else "150-400字",
        "dominant_type": types.most_common(1)[0][0] if types else "场景分享型",
    }


def analyze_publish_timing(notes: list[dict]) -> dict:
    """
    Analyze optimal publish timing based on high-engagement notes.
    """
    hour_scores = Counter()
    weekday_scores = Counter()

    for note in notes:
        published = note.get("published_at")
        if not published:
            continue
        try:
            if isinstance(published, str):
                dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            else:
                dt = published
            hour = (dt.hour + 8) % 24  # UTC -> CST
            weekday = dt.weekday()

            engagement = note.get("likes", 0) + note.get("collects", 0) + note.get("comments", 0)
            hour_scores[hour] += engagement
            weekday_scores[weekday] += engagement
        except Exception:
            pass

    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    best_hours = [h for h, _ in hour_scores.most_common(3)] if hour_scores else [20, 21, 12]
    best_days = [weekday_names[d] for d, _ in weekday_scores.most_common(3)] if weekday_scores else ["周二", "周四", "周六"]

    return {
        "best_hours": sorted(best_hours),
        "best_days": best_days,
        "recommendation": f"建议发布时段: {'/'.join(best_days)} {best_hours[0] if best_hours else 20}:00~{(best_hours[0]+2) if best_hours else 22}:00",
    }


def analyze_comment_intent(comments: list[dict]) -> dict:
    """
    Analyze purchase intent and sentiment from comments.
    """
    total = len(comments)
    if total == 0:
        return {"total": 0, "purchase_intent_ratio": 0, "positive_ratio": 0}

    purchase_intent = 0
    positive = 0

    for c in comments:
        text = c.get("text", "")
        if any(kw in text for kw in PURCHASE_INTENT_KEYWORDS):
            purchase_intent += 1
        if any(kw in text for kw in POSITIVE_FEEDBACK_KEYWORDS):
            positive += 1

    # Extract most-asked questions
    question_pattern = re.compile(r'[^。！？\n]*[？?]')
    questions = Counter()
    for c in comments:
        for q in question_pattern.findall(c.get("text", "")):
            q = q.strip()
            if 3 < len(q) < 50:
                questions[q] += 1

    return {
        "total_comments": total,
        "purchase_intent_count": purchase_intent,
        "purchase_intent_ratio": round(purchase_intent / total * 100, 1),
        "positive_count": positive,
        "positive_ratio": round(positive / total * 100, 1),
        "top_questions": [q for q, _ in questions.most_common(5)],
        "intent_keywords_found": list({
            kw for c in comments for kw in PURCHASE_INTENT_KEYWORDS if kw in c.get("text", "")
        }),
    }


def generate_category_report(
    keyword: str,
    category_data: dict,
    title_analysis: dict,
    structure_analysis: dict,
    timing_analysis: dict,
    comment_analysis: dict,
) -> dict:
    """
    Generate a comprehensive XHS category analysis report.
    """
    return {
        "keyword": keyword,
        "summary": {
            "total_notes_scanned": category_data.get("total_notes", 0),
            "avg_likes": category_data.get("avg_likes", 0),
            "top10_avg_likes": category_data.get("top10_avg_likes", 0),
            "product_note_ratio": category_data.get("product_note_ratio", 0),
            "purchase_intent_ratio": category_data.get("purchase_intent_ratio", 0),
        },
        "content_strategy": {
            "dominant_type": structure_analysis.get("dominant_type"),
            "recommended_length": structure_analysis.get("recommended_length"),
            "seed_words": title_analysis.get("seed_words_found", []),
            "top_keywords": list(title_analysis.get("top_keywords", {}).keys())[:10],
        },
        "timing": timing_analysis,
        "audience_intent": {
            "purchase_intent_ratio": comment_analysis.get("purchase_intent_ratio", 0),
            "positive_ratio": comment_analysis.get("positive_ratio", 0),
            "top_questions": comment_analysis.get("top_questions", []),
        },
        "reference_notes": category_data.get("detailed_notes", [])[:5],
        "generated_at": datetime.now().isoformat(),
    }
