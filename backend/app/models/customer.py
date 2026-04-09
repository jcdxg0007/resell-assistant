from datetime import datetime

from sqlalchemy import String, Text, Integer, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UUIDMixin, TimestampMixin


class Conversation(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "conversations"

    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False, index=True)
    buyer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    buyer_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    product_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    order_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")  # "active" / "resolved" / "escalated"
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")  # "low" / "normal" / "high" / "urgent"
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)  # "inquiry" / "bargain" / "shipping" / "after_sale" / "virtual_delivery"


class Message(Base, UUIDMixin):
    __tablename__ = "messages"

    conversation_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # "buyer" / "seller" / "ai_draft"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_type: Mapped[str] = mapped_column(String(16), nullable=False, default="text")  # "text" / "image" / "system"

    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_approved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReplyTemplate(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "reply_templates"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # "welcome" / "payment_reminder" / "shipping_notice" / "bargain" / "after_sale" / "review_request"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[list | None] = mapped_column(JSON, nullable=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
