from datetime import datetime

from sqlalchemy import String, Text, Float, Integer, DateTime, ForeignKey, JSON, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import UUIDMixin, TimestampMixin


class Order(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "orders"

    sale_platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # "xianyu" / "xiaohongshu"
    sale_order_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    account_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False, index=True)

    listing_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    product_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"), nullable=True)

    buyer_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    buyer_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    buyer_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    sale_price: Mapped[float] = mapped_column(Float, nullable=False)
    platform_fee: Mapped[float] = mapped_column(Float, default=0.0)
    purchase_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    shipping_cost: Mapped[float] = mapped_column(Float, default=0.0)
    actual_profit: Mapped[float | None] = mapped_column(Float, nullable=True)

    sku_info: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_sku_mapping: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Source platform purchase info
    source_platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_order_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    # pending / purchasing / purchased / shipped / delivered / completed / refunding / refunded / cancelled / error
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="physical")  # "physical" / "virtual"

    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    logistics: Mapped[list["Logistics"]] = relationship(back_populates="order", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_orders_status_platform", "status", "sale_platform"),
    )


class Logistics(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "logistics"

    order_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="forward")  # "forward" / "return"

    carrier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tracking_number: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    # pending / picked_up / in_transit / delivering / delivered / returned / lost
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    tracking_events: Mapped[list | None] = mapped_column(JSON, nullable=True)

    synced_to_sale_platform: Mapped[bool] = mapped_column(default=False)
    last_tracked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    order: Mapped["Order"] = relationship(back_populates="logistics")
