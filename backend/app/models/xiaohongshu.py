from datetime import datetime

from sqlalchemy import String, Text, Float, Integer, DateTime, ForeignKey, JSON, Boolean, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UUIDMixin, TimestampMixin


class XhsNote(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "xhs_notes"

    product_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True)
    account_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False, index=True)

    xhs_note_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    note_type: Mapped[str] = mapped_column(String(32), nullable=False)  # "seed_review" / "tutorial" / "collection" / "comparison" / "scene" / "avoid_trap"
    content_type: Mapped[str] = mapped_column(String(16), nullable=False, default="image")  # "image" / "video"

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    image_paths: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    topics: Mapped[list | None] = mapped_column(JSON, nullable=True)
    linked_product_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    # draft / scheduled / published / restricted / removed
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_monetized: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_xhs_notes_account_status", "account_id", "status"),
    )


class XhsNoteAnalytics(Base, UUIDMixin):
    __tablename__ = "xhs_note_analytics"

    note_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("xhs_notes.id", ondelete="CASCADE"), nullable=False, index=True)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    collects: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    views_estimated: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interaction_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_xhs_analytics_note_time", "note_id", "captured_at"),
    )


class XhsHotTopic(Base, UUIDMixin):
    __tablename__ = "xhs_hot_topics"

    topic_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    note_count: Mapped[int] = mapped_column(Integer, default=0)
    growth_rate_daily: Mapped[float | None] = mapped_column(Float, nullable=True)
    growth_rate_weekly: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_trending: Mapped[bool] = mapped_column(Boolean, default=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_xhs_hot_topics_trending", "is_trending", "captured_at"),
    )


class XhsTrendingKeyword(Base, UUIDMixin):
    __tablename__ = "xhs_trending_keywords"

    keyword: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # "hot_search" / "suggest" / "comment_mining"
    search_volume_estimated: Mapped[int | None] = mapped_column(Integer, nullable=True)
    growth_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    related_products_found: Mapped[bool] = mapped_column(Boolean, default=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class XhsCompetitorNote(Base, UUIDMixin):
    __tablename__ = "xhs_competitor_notes"

    keyword: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    xhs_note_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    author_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    cover_style: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_structure: Mapped[str | None] = mapped_column(String(64), nullable=True)

    likes: Mapped[int] = mapped_column(Integer, default=0)
    collects: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    interaction_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    has_product_link: Mapped[bool] = mapped_column(Boolean, default=False)

    purchase_intent_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class XhsShopProduct(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "xhs_shop_products"

    product_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False, index=True)

    xhs_product_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    sales_count: Mapped[int] = mapped_column(Integer, default=0)


class XhsContentTemplate(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "xhs_content_templates"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    template_type: Mapped[str] = mapped_column(String(32), nullable=False)  # "title" / "body" / "reply"
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[list | None] = mapped_column(JSON, nullable=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
