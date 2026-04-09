from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.customer import Conversation, Message, ReplyTemplate
from app.models.system import User
from app.services.customer.message_hub import generate_ai_reply, classify_intent

router = APIRouter()


@router.get("/conversations", summary="会话列表")
async def list_conversations(
    platform: str | None = None,
    status: str | None = "active",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(Conversation)
    if platform:
        query = query.where(Conversation.platform == platform)
    if status:
        query = query.where(Conversation.status == status)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(Conversation.last_message_at.desc().nulls_last()).offset((page - 1) * page_size).limit(page_size)
    convs = (await db.execute(query)).scalars().all()

    total_unread = (await db.execute(
        select(func.sum(Conversation.unread_count)).where(Conversation.status == "active")
    )).scalar() or 0

    return {
        "total": total,
        "total_unread": total_unread,
        "items": [
            {
                "id": str(c.id),
                "platform": c.platform,
                "buyer_name": c.buyer_name,
                "status": c.status,
                "priority": c.priority,
                "unread_count": c.unread_count,
                "intent": c.intent,
                "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
            }
            for c in convs
        ],
    }


@router.get("/conversations/{conv_id}/messages", summary="会话消息列表")
async def list_messages(
    conv_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Conversation).where(Conversation.id == conv_id))
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")

    query = select(Message).where(Message.conversation_id == conv_id).order_by(Message.sent_at.desc())
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.offset((page - 1) * page_size).limit(page_size)
    messages = (await db.execute(query)).scalars().all()

    # Mark as read
    conv.unread_count = 0
    await db.commit()

    return {
        "total": total,
        "conversation": {
            "id": str(conv.id),
            "platform": conv.platform,
            "buyer_name": conv.buyer_name,
            "intent": conv.intent,
        },
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "message_type": m.message_type,
                "ai_generated": m.ai_generated,
                "ai_approved": m.ai_approved,
                "sent_at": m.sent_at.isoformat() if m.sent_at else None,
            }
            for m in reversed(messages)
        ],
    }


class SendMessageRequest(BaseModel):
    content: str
    use_ai_draft: bool = False


@router.post("/conversations/{conv_id}/send", summary="发送回复")
async def send_message(
    conv_id: str,
    req: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Conversation).where(Conversation.id == conv_id))
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")

    message = Message(
        conversation_id=conv.id,
        role="seller",
        content=req.content,
        ai_generated=req.use_ai_draft,
        ai_approved=True if req.use_ai_draft else None,
        sent_at=datetime.now(timezone.utc),
    )
    db.add(message)
    await db.commit()

    # TODO: Actually send via Playwright to the platform
    return {"message": "回复已记录（需通过Playwright发送到平台）", "id": str(message.id)}


class AiReplyRequest(BaseModel):
    message: str
    intent: str | None = None


@router.post("/ai-reply", summary="生成AI预回复")
async def get_ai_reply(
    req: AiReplyRequest,
    user: User = Depends(get_current_user),
):
    intent = req.intent or classify_intent(req.message)
    reply = await generate_ai_reply(message=req.message, intent=intent)
    return {"intent": intent, "reply": reply}


@router.get("/templates", summary="话术模板列表")
async def list_templates(
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = select(ReplyTemplate).where(ReplyTemplate.is_active == True)
    if category:
        query = query.where(ReplyTemplate.category == category)
    query = query.order_by(ReplyTemplate.usage_count.desc())
    templates = (await db.execute(query)).scalars().all()

    return {
        "items": [
            {
                "id": str(t.id),
                "name": t.name,
                "category": t.category,
                "content": t.content,
                "variables": t.variables,
                "usage_count": t.usage_count,
            }
            for t in templates
        ],
    }
