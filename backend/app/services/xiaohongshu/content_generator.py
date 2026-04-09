"""
XHS content generation service.
Uses LLM to generate note drafts, titles, and tag recommendations.
"""
import random
from datetime import datetime

import httpx
from loguru import logger

from app.core.config import get_settings

settings = get_settings()

NOTE_TYPES = {
    "seed_review": {
        "name": "种草测评",
        "structure": "痛点 → 产品介绍 → 使用体验 → 推荐理由",
        "prompt_hint": "写一篇真实感强的种草测评笔记，像分享给闺蜜一样自然",
    },
    "tutorial": {
        "name": "教程干货",
        "structure": "场景/问题 → 需要的工具 → 详细步骤 → 效果展示",
        "prompt_hint": "写一篇手把手教程笔记，步骤清晰，小白也能看懂",
    },
    "collection": {
        "name": "合集盘点",
        "structure": "主题引入 → 逐个推荐(编号) → 总结对比",
        "prompt_hint": "写一篇好物合集笔记，每个品简短精炼突出亮点",
    },
    "comparison": {
        "name": "对比评测",
        "structure": "两款/多款产品 → 各维度对比 → 结论推荐",
        "prompt_hint": "写一篇客观的对比评测笔记，有数据有结论",
    },
    "scene": {
        "name": "场景展示",
        "structure": "使用场景描述 → 氛围感文字 → 产品融入",
        "prompt_hint": "写一篇有氛围感的场景笔记，让人向往这种生活方式",
    },
    "avoid_trap": {
        "name": "避坑指南",
        "structure": "踩坑经历 → 血泪教训 → 靠谱替代推荐",
        "prompt_hint": "写一篇避坑指南，先讲踩过的坑再推荐靠谱选择",
    },
}

TITLE_STYLES = [
    ("种草型", "突出产品魅力，用感叹号和emoji，如「必入！」「绝了」"),
    ("干货型", "突出实用性，如「保姆级教程」「手把手教你」"),
    ("悬念型", "引发好奇，如「后悔没早买」「用了就回不去」"),
    ("对比型", "突出性价比，如「平替XX」「吊打千元XX」"),
]


async def generate_note_titles(
    product_title: str,
    category: str = "",
    note_type: str = "seed_review",
    count: int = 5,
) -> list[dict]:
    """Generate candidate note titles using LLM with fallback templates."""
    type_info = NOTE_TYPES.get(note_type, NOTE_TYPES["seed_review"])

    if settings.LLM_API_KEY:
        try:
            prompt = f"""你是小红书爆款标题专家。为以下商品生成{count}个不同风格的笔记标题。

商品: {product_title}
品类: {category or '未指定'}
笔记类型: {type_info['name']}

要求:
1. 每个标题15-25字
2. 包含以下风格各1个: 种草型、干货型、悬念型、对比型、情感型
3. 使用小红书常见表达(如绝绝子/yyds/闭眼入/天花板等)
4. 适当使用emoji(1-2个)
5. 每行一个标题，不要序号

只输出标题，不要其他内容。"""

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{settings.LLM_API_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
                    json={
                        "model": settings.LLM_MODEL_LIGHT,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.9,
                        "max_tokens": 300,
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"].strip()
                import re
                lines = [re.sub(r'^[\d]+[.、)\]]\s*', '', l.strip()) for l in content.split("\n") if l.strip()]
                if len(lines) >= 3:
                    return [{"title": t, "style": TITLE_STYLES[i % len(TITLE_STYLES)][0]} for i, t in enumerate(lines[:count])]
        except Exception as e:
            logger.warning(f"LLM title generation failed: {e}")

    # Template fallback
    templates = [
        f"📷 {product_title}｜用过就回不去了",
        f"🔥 {product_title[:12]}必入好物推荐！绝绝子",
        f"💡 保姆级教程｜{product_title[:12]}怎么选不踩坑",
        f"✨ 平替{product_title[:10]}？这款性价比绝了",
        f"❤️ 入手{product_title[:12]}一周真实感受",
    ]
    return [{"title": t, "style": TITLE_STYLES[i % len(TITLE_STYLES)][0]} for i, t in enumerate(templates[:count])]


async def generate_note_body(
    product_title: str,
    category: str = "",
    note_type: str = "seed_review",
    selling_points: list[str] | None = None,
    source_reviews: list[str] | None = None,
) -> list[dict]:
    """Generate 2-3 note body drafts in different styles."""
    type_info = NOTE_TYPES.get(note_type, NOTE_TYPES["seed_review"])
    points_text = "\n".join(f"- {p}" for p in (selling_points or [])) or "（无具体卖点，请自由发挥）"
    reviews_text = "\n".join(f'"{r}"' for r in (source_reviews or [])[:3]) or "（无评价参考）"

    if settings.LLM_API_KEY:
        try:
            prompt = f"""你是小红书内容创作者。为以下商品写一篇{type_info['name']}笔记正文。

商品: {product_title}
品类: {category}
笔记结构: {type_info['structure']}
风格要求: {type_info['prompt_hint']}

商品卖点:
{points_text}

买家评价参考:
{reviews_text}

要求:
1. 300-500字
2. 小红书口语化风格，像朋友聊天
3. 适当使用emoji(不要太多)
4. 分段清晰，有小标题
5. 结尾带"你们觉得呢？"或类似互动引导
6. 不要出现广告感太强的表达"""

            async with httpx.AsyncClient(timeout=45) as client:
                results = []
                for temp in [0.7, 0.9]:
                    resp = await client.post(
                        f"{settings.LLM_API_BASE_URL}/chat/completions",
                        headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
                        json={
                            "model": settings.LLM_MODEL_LIGHT,
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": temp,
                            "max_tokens": 600,
                        },
                    )
                    resp.raise_for_status()
                    body = resp.json()["choices"][0]["message"]["content"].strip()
                    results.append({
                        "body": body,
                        "note_type": note_type,
                        "word_count": len(body),
                    })
                return results
        except Exception as e:
            logger.warning(f"LLM body generation failed: {e}")

    # Template fallback
    template = f"""🎯 分享一个我最近入手的好物！

最近一直在找{category or '这类'}的好用装备，试了好几个终于找到了满意的 ——

✨ {product_title}

{points_text}

实际使用下来真的超出预期！{(source_reviews or ['质量很好'])[0]}

📝 总结：
性价比真的很高，推荐给需要的姐妹/兄弟们～

你们有用过类似的吗？评论区聊聊👇"""

    return [{"body": template, "note_type": note_type, "word_count": len(template)}]


async def recommend_tags(
    product_title: str,
    category: str = "",
    count: int = 10,
) -> list[dict]:
    """Recommend hashtags based on product and category."""
    base_tags = []

    category_tag_map = {
        "3C": ["数码好物", "科技控", "装备党", "数码推荐", "电子产品"],
        "摄影": ["摄影装备", "相机配件", "摄影技巧", "拍照技巧", "Vlog装备"],
        "家居": ["家居好物", "桌面收纳", "生活好物", "家居装饰", "收纳神器"],
        "教育": ["考研", "备考", "学习资料", "考试", "提分"],
        "职场": ["职场干货", "PPT", "效率工具", "办公好物", "职场技能"],
    }

    for cat, tags in category_tag_map.items():
        if cat in (category or ""):
            base_tags.extend(tags)
            break

    base_tags.extend(["好物分享", "好物推荐"])

    if settings.LLM_API_KEY:
        try:
            prompt = f"为小红书笔记推荐{count}个相关话题标签，商品是「{product_title}」，品类是「{category}」。每行一个标签，不带#号，不要序号。"
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{settings.LLM_API_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
                    json={
                        "model": settings.LLM_MODEL_LIGHT,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.7,
                        "max_tokens": 150,
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"].strip()
                llm_tags = [l.strip().lstrip('#') for l in content.split("\n") if l.strip()]
                base_tags = llm_tags + base_tags
        except Exception:
            pass

    seen = set()
    unique_tags = []
    for t in base_tags:
        if t not in seen:
            seen.add(t)
            unique_tags.append({"tag": t, "source": "ai" if t not in category_tag_map else "preset"})
    return unique_tags[:count]


def suggest_cover_styles(category: str = "", note_type: str = "seed_review") -> list[dict]:
    """Suggest cover image styles based on category and note type."""
    styles = [
        {"style": "产品主体大图", "description": "产品居中，干净背景，大字标题覆盖", "suitable_for": ["seed_review", "scene"]},
        {"style": "使用前后对比", "description": "左右分栏，对比效果明显", "suitable_for": ["comparison", "tutorial"]},
        {"style": "多图拼接", "description": "4宫格或6宫格展示多角度/多功能", "suitable_for": ["collection", "seed_review"]},
        {"style": "场景实拍", "description": "产品融入使用场景，氛围感强", "suitable_for": ["scene", "seed_review"]},
        {"style": "教程步骤图", "description": "步骤标注清晰，有指引箭头", "suitable_for": ["tutorial", "avoid_trap"]},
        {"style": "大字报风格", "description": "极简背景+大号醒目文字", "suitable_for": ["avoid_trap", "collection"]},
    ]

    relevant = [s for s in styles if note_type in s["suitable_for"]]
    others = [s for s in styles if note_type not in s["suitable_for"]]
    return (relevant + others)[:4]
