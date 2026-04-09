from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    String, Text, Float, Integer, Boolean, DateTime, ForeignKey, Enum, Index, JSON
)
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import UUIDMixin, TimestampMixin


class Platform(str, PyEnum):
    PINDUODUO = "pinduoduo"
    TAOBAO = "taobao"
    XIANYU = "xianyu"
    XIAOHONGSHU = "xiaohongshu"
    DOUYIN = "douyin"


class ProductType(str, PyEnum):
    PHYSICAL = "physical"
    VIRTUAL = "virtual"


class Product(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "products"

    source_platform: Mapped[str] = mapped_column(Enum(Platform), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=True, index=True)
    product_type: Mapped[str] = mapped_column(
        Enum(ProductType), nullable=False, default=ProductType.PHYSICAL
    )

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    original_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    shipping_fee: Mapped[float] = mapped_column(Float, default=0.0)
    image_urls: Mapped[list | None] = mapped_column(JSON, nullable=True)

    sku_info: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sales_count: Mapped[int] = mapped_column(Integer, default=0)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    price_snapshots: Mapped[list["PriceSnapshot"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    images: Mapped[list["ProductImage"]] = relationship(back_populates="product", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_products_source_platform_source_id", "source_platform", "source_id", unique=True),
        Index("ix_products_category_active", "category", "is_active"),
    )


class PriceSnapshot(Base, UUIDMixin):
    __tablename__ = "price_snapshots"

    product_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    sales_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    product: Mapped["Product"] = relationship(back_populates="price_snapshots")

    __table_args__ = (
        Index("ix_price_snapshots_product_time", "product_id", "captured_at"),
    )


class ProductMatch(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "product_matches"

    source_product_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    target_product_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    target_platform: Mapped[str] = mapped_column(Enum(Platform), nullable=False)

    text_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    phash_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    clip_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)

    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_product_matches_source_target", "source_product_id", "target_product_id", unique=True),
    )


class ProductScore(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "product_scores"

    product_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    score_type: Mapped[str] = mapped_column(String(32), nullable=False)  # "xianyu_10d" or "xhs_5d"
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    dimension_scores: Mapped[dict] = mapped_column(JSON, nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)  # "strong_recommend" / "worth_try" / "average" / "skip"
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_product_scores_type_score", "score_type", "total_score"),
    )


class ImageSource(str, PyEnum):
    SOURCE_MAIN = "source_main"
    SOURCE_DETAIL = "source_detail"
    SOURCE_REVIEW = "source_review"
    USER_UPLOAD = "user_upload"
    PROCESSED = "processed"


class ProductImage(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "product_images"

    product_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(Enum(ImageSource), nullable=False)
    original_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    phash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    clip_vector: Mapped[list | None] = mapped_column(JSON, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False)

    product: Mapped["Product"] = relationship(back_populates="images")
