"""
Customer message aggregation hub.
Collects messages from all platforms and generates AI pre-replies.
"""
import asyncio
import random
import re
from datetime import datetime, timezone

import httpx
from loguru import logger

from app.core.config import get_settings
from app.services.browser import browser_manager
from app.services.notification import notification_service

settings = get_settings()

INTENT_PATTERNS = {
    "inquiry": ["兼容", "参数", "规格", "支持", "适配", "能用", "可以用", "型号", "尺寸", "材质"],
    "bargain": ["便宜", "优惠", "包邮", "少点", "最低", "打折"],
    "shipping": ["发货", "什么时候", "快递", "物流", "到了吗", "到哪了"],
    "after_sale": ["退货", "退款", "质量", "坏了", "破损", "不满意", "差评", "投诉"],
    "virtual_delivery": ["下载", "链接", "没收到", "怎么获取", "在哪下", "资源"],
}

INTENT_PRIORITY = {
    "after_sale": "high",
    "shipping": "medium",
    "inquiry": "medium",
    "bargain": "low",
    "virtual_delivery": "high",
    "unknown": "low",
}

BARGAIN_RESPONSES = [
    "亲，这个已经是最低价啦～品质有保证的哦",
    "亲，价格很实在了呢，质量您放心～",
    "不好意思亲，已经是底价了，薄利多销～",
]


def classify_intent(message: str) -> str:
    """Classify customer message intent."""
    for intent, keywords in INTENT_PATTERNS.items():
        if any(kw in message for kw in keywords):
            return intent
    return "unknown"


async def generate_ai_reply(
    message: str,
    intent: str,
    product_info: dict | None = None,
    conversation_history: list[dict] | None = None,
) -> str:
    """Generate an AI pre-reply draft using LLM."""
    if intent == "bargain":
        return random.choice(BARGAIN_RESPONSES)

    if not settings.LLM_API_KEY:
        return _template_reply(intent, product_info)

    product_context = ""
    if product_info:
        product_context = f"""
商品信息:
- 名称: {product_info.get('title', '')}
- 售价: ¥{product_info.get('price', '')}
- 描述: {product_info.get('description', '')[:200]}"""

    history_text = ""
    if conversation_history:
        history_text = "\n".join(
            f"{'买家' if m.get('role') == 'buyer' else '卖家'}: {m.get('text', '')}"
            for m in conversation_history[-5:]
        )

    prompt = f"""你是闲鱼/小红书卖家客服助手。根据买家消息生成一条友好、专业的回复。

买家消息: "{message}"
消息意图: {intent}
{product_context}
{f"对话历史:{history_text}" if history_text else ""}

要求:
1. 口语化、亲切、简短(50字内)
2. 如果是咨询兼容性/参数，基于商品信息回答
3. 如果是催发货，说"已安排发货，请耐心等待～"
4. 如果是售后，安抚情绪，说"帮您处理"
5. 只输出回复内容，不要其他"""

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.LLM_API_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
                json={
                    "model": settings.LLM_MODEL_LIGHT,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.6,
                    "max_tokens": 100,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"AI reply generation failed: {e}")
        return _template_reply(intent, product_info)


def _template_reply(intent: str, product_info: dict | None = None) -> str:
    """Fallback template-based replies."""
    templates = {
        "inquiry": "亲，这款商品{detail}，有其他问题可以随时问我哦～",
        "bargain": random.choice(BARGAIN_RESPONSES),
        "shipping": "亲，已经安排发货啦，预计1-3天到货，请耐心等待～",
        "after_sale": "亲，非常抱歉给您带来不好的体验，我来帮您处理～",
        "virtual_delivery": "亲，链接已经发给您啦，请查看聊天记录～如果没收到我重新发一次",
        "unknown": "亲，收到～请问有什么可以帮到您的？",
    }
    reply = templates.get(intent, templates["unknown"])
    if product_info and "{detail}" in reply:
        reply = reply.replace("{detail}", f"兼容{product_info.get('title', '多种型号')}")
    return reply


async def collect_xianyu_messages(
    account_id: str,
    account_config: dict,
    known_message_ids: set[str],
) -> list[dict]:
    """Collect new messages from Xianyu chat."""
    context = await browser_manager.get_context(account_id, account_config)
    page = await context.new_page()
    messages = []

    try:
        await page.goto(
            "https://www.goofish.com/message",
            wait_until="networkidle",
            timeout=30000,
        )
        await asyncio.sleep(random.uniform(2, 4))

        conv_items = await page.query_selector_all('[class*="conversation"], [class*="chat-item"]')

        for conv in conv_items[:20]:
            try:
                unread = await conv.query_selector('[class*="unread"], [class*="badge"]')
                if not unread:
                    continue

                name_el = await conv.query_selector('[class*="name"]')
                preview_el = await conv.query_selector('[class*="preview"], [class*="last-msg"]')
                time_el = await conv.query_selector('[class*="time"]')

                buyer_name = (await name_el.inner_text()).strip() if name_el else ""
                preview = (await preview_el.inner_text()).strip() if preview_el else ""
                msg_time = (await time_el.inner_text()).strip() if time_el else ""

                msg_id = f"xy_{buyer_name}_{preview[:20]}"
                if msg_id in known_message_ids:
                    continue

                intent = classify_intent(preview)
                priority = INTENT_PRIORITY.get(intent, "low")

                messages.append({
                    "msg_id": msg_id,
                    "platform": "xianyu",
                    "buyer_name": buyer_name,
                    "last_message": preview,
                    "intent": intent,
                    "priority": priority,
                    "time": msg_time,
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                })

                if priority == "high":
                    await notification_service.send_dingtalk(
                        title=f"🚨 紧急消息 [{buyer_name}]",
                        content=f"平台: 闲鱼\n买家: {buyer_name}\n消息: {preview}\n意图: {intent}",
                    )

            except Exception as e:
                logger.debug(f"Message parse error: {e}")

        await browser_manager.save_state(account_id)
        return messages

    except Exception as e:
        logger.error(f"Message collection failed: {e}")
        return messages
    finally:
        await page.close()
