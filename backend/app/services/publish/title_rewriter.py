"""
Title rewriting engine for Xianyu listing differentiation.
Combines rule-based synonym replacement with optional LLM rewriting.
"""
import random
import re
from typing import Any

import httpx
from loguru import logger

from app.core.config import settings

SYNONYM_MAP = {
    "全新": ["崭新", "未拆封", "未使用", "新品"],
    "正品": ["保真", "官方正版", "原装"],
    "包邮": ["顺丰包邮", "免运费", "送运费险"],
    "超值": ["划算", "超高性价比", "好价"],
    "便宜": ["实惠", "亲民价", "学生党福音"],
    "好用": ["超好用", "实测好用", "亲测推荐"],
    "高品质": ["质量很好", "用料扎实", "做工精细"],
    "转让": ["出", "低价转", "割爱"],
    "闲置": ["二手", "个人出", "自用"],
    "现货": ["秒发", "当天发", "有库存"],
}

SELLING_POINTS = [
    "买到赚到", "入股不亏", "自用推荐", "回购无数次",
    "颜值超高", "性价比之王", "闭眼入", "宝藏好物",
    "超实用", "送人自用都合适", "刚需入",
]

XY_SEO_PREFIXES = [
    "", "【在售】", "【特价】", "【正品】", "【现货】",
]


def rule_based_rewrite(title: str, category: str | None = None) -> list[str]:
    """Generate 3-5 title variants using synonym replacement and restructuring."""
    variants = set()

    for _ in range(5):
        new_title = title
        for word, synonyms in SYNONYM_MAP.items():
            if word in new_title and random.random() > 0.4:
                new_title = new_title.replace(word, random.choice(synonyms), 1)

        if random.random() > 0.5 and len(new_title) < 25:
            new_title = f"{new_title} {random.choice(SELLING_POINTS)}"

        if random.random() > 0.6:
            prefix = random.choice(XY_SEO_PREFIXES)
            if prefix and not new_title.startswith("【"):
                new_title = f"{prefix}{new_title}"

        new_title = new_title.strip()
        if new_title and new_title != title:
            variants.add(new_title)

    return list(variants)[:5]


def restructure_title(title: str) -> str:
    """Restructure title by rearranging segments."""
    separators = r'[/\-|·,，、]'
    parts = re.split(separators, title)
    parts = [p.strip() for p in parts if p.strip()]

    if len(parts) >= 3:
        random.shuffle(parts)
        sep = random.choice([" ", "/", " "])
        return sep.join(parts)
    return title


async def llm_rewrite(title: str, category: str | None = None, count: int = 3) -> list[str]:
    """Use LLM API to generate differentiated title variants."""
    if not settings.LLM_API_KEY:
        logger.debug("LLM API key not configured, skipping LLM rewrite")
        return []

    prompt = f"""你是一个闲鱼商品标题优化专家。请帮我改写以下商品标题，生成{count}个不同版本。

要求：
1. 保留核心卖点和关键参数（型号、尺寸等）
2. 用不同的表达方式，避免重复
3. 加入适当的闲鱼SEO关键词
4. 每个标题 15-30 字
5. 口语化、接地气
6. 不要使用emoji
{f'7. 商品品类: {category}' if category else ''}

原标题: {title}

请直接输出改写后的标题，每行一个，不要编号："""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                settings.LLM_API_BASE + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.LLM_MODEL_LIGHT,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.8,
                    "max_tokens": 300,
                },
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                lines = [l.strip() for l in content.strip().split("\n") if l.strip()]
                lines = [re.sub(r'^[\d]+[.、)\]]\s*', '', l) for l in lines]
                return [l for l in lines if 5 <= len(l) <= 40][:count]
    except Exception as e:
        logger.error(f"LLM title rewrite failed: {e}")
    return []


async def generate_title_variants(
    original_title: str,
    category: str | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """
    Generate differentiated title variants.
    Returns both rule-based and LLM variants.
    """
    rule_variants = rule_based_rewrite(original_title, category)

    restructured = restructure_title(original_title)
    if restructured != original_title:
        rule_variants.insert(0, restructured)

    llm_variants = []
    if use_llm:
        llm_variants = await llm_rewrite(original_title, category, count=3)

    all_variants = []
    seen = set()
    for v in llm_variants + rule_variants:
        normalized = v.strip().lower()
        if normalized not in seen and normalized != original_title.strip().lower():
            seen.add(normalized)
            all_variants.append(v)

    return {
        "original": original_title,
        "recommended": all_variants[0] if all_variants else original_title,
        "variants": all_variants[:6],
        "rule_based_count": len(rule_variants),
        "llm_count": len(llm_variants),
    }


def generate_description(
    title: str,
    category: str | None = None,
    specs: dict | None = None,
    price: float | None = None,
) -> str:
    """Generate a listing description template."""
    lines = []

    if specs:
        spec_text = "\n".join([f"· {k}: {v}" for k, v in specs.items()])
        lines.append(f"📋 参数信息\n{spec_text}")

    if category:
        lines.append(f"分类: {category}")

    lines.append("\n⚠️ 温馨提示")
    lines.append("· 非质量问题不支持退货退款")
    lines.append("· 拍前请确认型号/规格")
    lines.append("· 下单后24小时内发货")

    if price and price >= 50:
        lines.append("· 支持验货，放心购买")

    return "\n".join(lines)
