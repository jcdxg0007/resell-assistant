from datetime import datetime

from sqlalchemy import String, Text, Float, Integer, DateTime, ForeignKey, JSON, Boolean, Enum, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UUIDMixin, TimestampMixin


class XianyuListing(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "xianyu_listings"

    product_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False, index=True)

    xianyu_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    original_cost: Mapped[float] = mapped_column(Float, nullable=False)
    expected_profit: Mapped[float] = mapped_column(Float, nullable=False)

    image_paths: Mapped[list | None] = mapped_column(JSON, nullable=True)
    sku_mapping: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    # draft / pending_review / published / sold_out / removed / error
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    views: Mapped[int] = mapped_column(Integer, default=0)
    wants: Mapped[int] = mapped_column(Integer, default=0)
    chats: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("ix_xianyu_listings_account_status", "account_id", "status"),
    )


class XianyuMarketData(Base, UUIDMixin):
    __tablename__ = "xianyu_market_data"

    product_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    keyword: Mapped[str] = mapped_column(String(256), nullable=False, index=True)

    active_listings: Mapped[int] = mapped_column(Integer, default=0)
    total_wants: Mapped[int] = mapped_column(Integer, default=0)
    price_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_cv: Mapped[float | None] = mapped_column(Float, nullable=True)
    top5_sales: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    seller_distribution: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_listings_7d: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_listings_7d: Mapped[int | None] = mapped_column(Integer, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_xianyu_market_product_time", "product_id", "captured_at"),
    )
