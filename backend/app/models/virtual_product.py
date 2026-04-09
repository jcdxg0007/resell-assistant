from datetime import datetime

from sqlalchemy import String, Text, Float, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UUIDMixin, TimestampMixin


class VirtualProduct(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "virtual_products"

    product_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    delivery_type: Mapped[str] = mapped_column(String(32), nullable=False)  # "pan_link" / "card_key" / "file"
    delivery_content: Mapped[str] = mapped_column(Text, nullable=False)  # URL or card key
    delivery_message: Mapped[str] = mapped_column(Text, nullable=False)  # message template sent to buyer
    backup_links: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    total_sold: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class VirtualDelivery(Base, UUIDMixin):
    __tablename__ = "virtual_deliveries"

    order_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    virtual_product_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("virtual_products.id", ondelete="CASCADE"),
        nullable=False,
    )
    delivered_content: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivery_status: Mapped[str] = mapped_column(String(32), nullable=False, default="sent")  # "sent" / "confirmed" / "failed"
