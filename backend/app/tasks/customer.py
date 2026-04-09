"""Customer message collection Celery tasks."""
import asyncio
from loguru import logger
from sqlalchemy import select
from app.core.celery_app import celery_app
from app.core.database import AsyncSessionLocal
from app.models.system import Account
from app.models.customer import Conversation, Message
from app.services.customer.message_hub import collect_xianyu_messages, classify_intent, generate_ai_reply


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.customer.check_messages")
def check_messages():
    """Poll all platforms for new customer messages (every 3 min)."""
    logger.info("Starting customer message check")
    run_async(_check_messages())


async def _check_messages():
    from app.services.browser import browser_manager

    async with AsyncSessionLocal() as db:
        accounts = (await db.execute(
            select(Account).where(
                Account.platform.in_(["xianyu", "xiaohongshu"]),
                Account.is_active == True,
                Account.lifecycle_stage.in_(["growing", "mature"]),
            )
        )).scalars().all()

        for account in accounts:
            if account.platform != "xianyu":
                continue

            try:
                existing_msgs = await db.execute(
                    select(Message.external_msg_id).where(
                        Message.external_msg_id.isnot(None)
                    ).limit(500)
                )
                known_ids = {r[0] for r in existing_msgs.all()}

                config = {
                    "proxy_url": account.proxy_url,
                    "user_agent": account.user_agent,
                }

                new_messages = await collect_xianyu_messages(
                    account_id=str(account.id),
                    account_config=config,
                    known_message_ids=known_ids,
                )

                for msg_data in new_messages:
                    # Find or create conversation
                    conv_q = await db.execute(
                        select(Conversation).where(
                            Conversation.platform == msg_data["platform"],
                            Conversation.buyer_name == msg_data["buyer_name"],
                            Conversation.account_id == account.id,
                        )
                    )
                    conv = conv_q.scalar_one_or_none()
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc)

                    if not conv:
                        conv = Conversation(
                            platform=msg_data["platform"],
                            account_id=account.id,
                            buyer_id=msg_data.get("buyer_name", "unknown"),
                            buyer_name=msg_data["buyer_name"],
                            status="active",
                            priority="high" if msg_data["priority"] == "high" else "normal",
                            unread_count=0,
                            intent=msg_data["intent"],
                        )
                        db.add(conv)
                        await db.flush()

                    message = Message(
                        conversation_id=conv.id,
                        role="buyer",
                        content=msg_data["last_message"],
                        sent_at=now,
                    )
                    db.add(message)

                    conv.unread_count = (conv.unread_count or 0) + 1
                    conv.last_message_at = now
                    conv.intent = msg_data["intent"]

                    ai_reply = await generate_ai_reply(
                        message=msg_data["last_message"],
                        intent=msg_data["intent"],
                    )
                    ai_msg = Message(
                        conversation_id=conv.id,
                        role="ai_draft",
                        content=ai_reply,
                        ai_generated=True,
                        sent_at=now,
                    )
                    db.add(ai_msg)

                if new_messages:
                    await db.commit()
                    logger.info(f"Account {account.account_name}: {len(new_messages)} new messages")

            except Exception as e:
                logger.error(f"Message check failed for {account.account_name}: {e}")
