"""
Title and description rewriting for Xianyu listings.
Uses LLM for intelligent rewriting + fallback template-based rewriting.
"""
import random
import re

import httpx
from loguru import logger

from app.core.config import get_settings

settings = get_settings()

SYNONYM_MAP = {
    "全新": ["崭新", "未拆封", "刚到手", "Brand New"],
    "正品": ["保真", "专柜同款", "正版", "官方"],
    "包邮": ["顺丰包邮", "免运费", "邮费已含"],
    "低价": ["亏本出", "超值", "白菜价", "清仓"],
    "转让": ["出手", "闲置出", "诚意出", "自用闲置"],
    "二手": ["闲置", "个人出", "自用", "九成新"],
    "配件": ["周边", "附件", "搭配", "相关"],
    "支架": ["底座", "托架", "架子", "固定器"],
    "补光灯": ["摄影灯", "直播灯", "美颜灯", "打光灯"],
    "投影仪": ["投影", "便携投影", "微型投影", "家用投影"],
}

SELLING_POINT_TEMPLATES = [
    "🔥 {title}",
    "【闲置转让】{title}",
    "{title} 到手即用",
    "{title} 自用好物推荐",
    "清仓 {title} 不议价",
    "急出 {title} 超值",
    "{title} 性价比之王",
    "好物分享 {title}",
]

DESCRIPTION_TEMPLATE = """闲置转让，{condition}

{features}

📦 发货说明：拍下后48小时内发货
⚠️ 非质量问题不支持退货退款
💬 有问题可以先私聊"""


def _synonym_replace(title: str) -> str:
    """Replace random keywords with synonyms."""
    result = title
    for word, synonyms in SYNONYM_MAP.items():
        if word in result and random.random() > 0.5:
            result = result.replace(word, random.choice(synonyms), 1)
    return result


def _reorder_title_segments(title: str) -> str:
    """Slightly reorder title segments separated by spaces or common delimiters."""
    separators = r'[\s/|·,，、]+'
    parts = [p.strip() for p in re.split(separators, title) if p.strip()]
    if len(parts) < 2:
        return title
    core = parts[0]
    rest = parts[1:]
    random.shuffle(rest)
    return f"{core} {' '.join(rest)}"


def _add_selling_point(title: str) -> str:
    """Wrap title with a selling point template."""
    template = random.choice(SELLING_POINT_TEMPLATES)
    return template.format(title=title)


def template_rewrite_title(original_title: str) -> list[str]:
    """Generate 3 title variants using template-based rewriting."""
    variants = set()

    v1 = _synonym_replace(original_title)
    variants.add(v1)

    v2 = _reorder_title_segments(original_title)
    v2 = _synonym_replace(v2)
    variants.add(v2)

    v3 = _add_selling_point(original_title[:20])
    variants.add(v3)

    # Ensure at least 3
    while len(variants) < 3:
        v = _synonym_replace(_reorder_title_segments(original_title))
        v = _add_selling_point(v[:25]) if random.random() > 0.5 else v
        variants.add(v)

    return list(variants)[:3]


def generate_description(
    title: str,
    condition: str = "功能完好",
    features: str = "",
    return_policy: str = "非质量问题不支持退货退款",
) -> str:
    """Generate a listing description from template."""
    return DESCRIPTION_TEMPLATE.format(
        condition=condition,
        features=features or f"商品：{title}\n成色：{condition}",
    )


async def llm_rewrite_title(original_title: str, category: str = "") -> list[str]:
    """Use LLM to generate title variants (requires API key)."""
    if not settings.LLM_API_KEY:
        logger.debug("LLM API key not configured, falling back to template rewriting")
        return template_rewrite_title(original_title)

    prompt = f"""你是闲鱼标题优化专家。请为以下商品生成3个不同的闲鱼标题变体。
要求：
1. 保留核心关键词（品牌/型号/功能）
2. 每个标题风格不同（信息型/卖点型/搜索型）
3. 长度20-30字
4. 适合闲鱼搜索SEO
5. 只输出3个标题，每行一个，不要序号

原标题: {original_title}
品类: {category or '未指定'}"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.LLM_API_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
                json={
                    "model": settings.LLM_MODEL_LIGHT,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.8,
                    "max_tokens": 200,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            lines = [l.strip() for l in content.split("\n") if l.strip()]
            lines = [re.sub(r'^[\d]+[.、)\]]\s*', '', l) for l in lines]
            if len(lines) >= 2:
                return lines[:3]
    except Exception as e:
        logger.warning(f"LLM title rewrite failed: {e}")

    return template_rewrite_title(original_title)


async def llm_generate_description(
    title: str,
    source_description: str = "",
    category: str = "",
) -> str:
    """Use LLM to generate an optimized listing description."""
    if not settings.LLM_API_KEY:
        return generate_description(title)

    prompt = f"""为闲鱼商品写一段转让描述，要求：
1. 200字以内，口语化，像个人转让
2. 突出商品卖点和使用感受
3. 末尾加上"⚠️ 非质量问题不支持退货退款"
4. 不要过度营销

商品: {title}
品类: {category or '未指定'}
源描述参考: {source_description[:200] if source_description else '无'}"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.LLM_API_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
                json={
                    "model": settings.LLM_MODEL_LIGHT,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                    "max_tokens": 300,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"LLM description generation failed: {e}")

    return generate_description(title)
