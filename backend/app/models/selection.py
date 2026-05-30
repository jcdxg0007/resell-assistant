"""
Selection-module data model (P1 of the product-selection architecture).

Hierarchy (large to small):

    Category ─1..N─► Keyword ─N..N─► Product  (linked via KeywordProduct)
                       │
                       └─1..N─► KeywordScore  (keyword-level market score)

Product-level scores continue to live in product_scores (already existing).
"""
from datetime import datetime

from sqlalchemy import (
    String, Integer, Float, Boolean, DateTime, ForeignKey, JSON,
    UniqueConstraint, Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import UUIDMixin, TimestampMixin


class Category(Base, UUIDMixin, TimestampMixin):
    """Top-level niche bucket, e.g. '相机配件', '小电器'."""
    __tablename__ = "selection_categories"

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    niche_hint: Mapped[str | None] = mapped_column(String(256), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    keywords: Mapped[list["Keyword"]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class Keyword(Base, UUIDMixin, TimestampMixin):
    """A search keyword under a category, e.g. 'action4' under '相机配件'."""
    __tablename__ = "selection_keywords"

    category_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("selection_categories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    text: Mapped[str] = mapped_column(String(128), nullable=False)

    # Platforms to crawl for this keyword. Defaults to all four at runtime.
    target_platforms: Mapped[list | None] = mapped_column(JSON, nullable=True)
    max_items_per_platform: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_crawled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── PDD 调度专属字段 ──────────────────────────────────────────
    # last_crawled_at 是跨平台共享的时间戳，PDD 这边需要自己独立的
    # "上次 PDD 跑过的时间" + "上次 PDD 结果"才能做轮播调度。
    # 添加这组字段的 migration: b8c9d0e1f2g3 (2026-05-28)
    pdd_last_searched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 最近一次 PDD 任务的结果：ok / empty / risk_blocked / failed
    pdd_last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 该词在 PDD 上跑什么模式：fast / list_deep / detail_smart / detail_deep
    # 详情见 docs/PDD-自建采集-roadmap.md §3 (Phase 2 详情页采集)
    pdd_mode: Mapped[str] = mapped_column(
        String(16), default="fast", nullable=False, server_default="fast"
    )
    # 是否安全词。FALSE = 即使 schedule_enabled=TRUE 也被 fire_from_lib 跳过。
    # 用于永久禁用敏感词（美瞳 / 医美 / 烟草 等 PDD 风控偏紧的品类）
    pdd_safe: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default="true"
    )
    # 累计 PDD 搜索次数（监控用，调度也可以用来选久未跑的词）
    pdd_searches_total: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )

    # ── 闲鱼 调度专属字段（与 PDD 一组对称，迁移 a1b2c3d4e5f6）──
    # 让闲鱼自动采集也走词库：xianyu_safe=FALSE 的词不参与闲鱼自动跑批。
    xianyu_safe: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default="true"
    )
    xianyu_last_searched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    xianyu_last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    xianyu_searches_total: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )

    category: Mapped["Category"] = relationship(back_populates="keywords")
    products: Mapped[list["KeywordProduct"]] = relationship(
        back_populates="keyword", cascade="all, delete-orphan"
    )
    scores: Mapped[list["KeywordScore"]] = relationship(
        back_populates="keyword", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("category_id", "text", name="uq_keyword_category_text"),
    )


class KeywordProduct(Base, UUIDMixin, TimestampMixin):
    """Many-to-many link between a keyword search and the products it surfaced.

    The same Product can appear under multiple keywords (e.g. a selfie stick
    shows up for both 'action4' and '运动相机自拍杆'). Display-side de-dup
    is done in the recommendation API (P4) by picking the keyword where the
    product ranks highest.
    """
    __tablename__ = "keyword_products"

    keyword_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("selection_keywords.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    first_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Rank of this product in the most recent search for this keyword
    # (1 = first result). Used by the recommendation UI to sort and de-dup.
    last_rank_in_search: Mapped[int | None] = mapped_column(Integer, nullable=True)

    keyword: Mapped["Keyword"] = relationship(back_populates="products")

    __table_args__ = (
        UniqueConstraint("keyword_id", "product_id", name="uq_kw_product"),
    )


class SelectionAnalysis(Base, UUIDMixin, TimestampMixin):
    """「十维度选品」页的实时打分缓存（按关键词一行）。

    打分是 on-demand 触发（打开页/点关键词时算），结果落这里缓存，
    同词再打开秒出；「重新分析」按钮强制重算覆盖。

    payload 都是 JSON：
      xianyu_payload —— A 闲鱼端排序结果 + 维度 + 价格分布
      pdd_payload    —— B PDD 端排序结果
      arbitrage      —— C 跨平台套利结论
    """
    __tablename__ = "selection_analysis"

    keyword: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    xianyu_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pdd_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    arbitrage: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class KeywordScore(Base, UUIDMixin, TimestampMixin):
    """Keyword-level (market) score — how good this keyword is to sell into.

    Separate from product_scores, which grades individual items within the
    keyword. See app/services/selection/scoring.py (refactored in P2) for
    which dimensions feed which score.
    """
    __tablename__ = "keyword_scores"

    keyword_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("selection_keywords.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    dimension_scores: Mapped[dict] = mapped_column(JSON, nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    scored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    keyword: Mapped["Keyword"] = relationship(back_populates="scores")

    __table_args__ = (
        Index("ix_keyword_scores_kw_time", "keyword_id", "scored_at"),
    )
