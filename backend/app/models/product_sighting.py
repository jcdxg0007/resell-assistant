"""跨天「同款观测」落库（Phase 1：精确指纹 L1）。

为什么单开一张表：闲鱼商品每天清库、PDD 商品只活在 pdd_search_runs 的 JSON 快照里，
两者都没有「跨天稳定身份」。这张表用稳定指纹 item_key 把同一个挂牌/快照在不同
「逻辑日」的出现各记一条，从而能算出「这个商品出现过几天、首次/最近何时、每天什么
价/什么热度」——给选品页做「持续在售」标签和价格趋势 mini 图。

身份指纹 item_key（见 services/selection/sightings.py）：
  - 闲鱼：xy:<source_id>（闲鱼 item_id 本身稳定）
  - PDD ：pdd:<sha1(clean_title)[:32]>（PDD 无稳定 id，用归一化标题指纹）

唯一键 (item_key, seen_date)：一个逻辑日一条，当天重复抓只刷新当天那条。
保留：复用统一的流水保留天数，按 created_at 物理清理（见 compliance）。

migration: product_sighting_01 (2026-06-06)
"""
from datetime import datetime, date

from sqlalchemy import String, Integer, Float, Date, Text, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UUIDMixin, TimestampMixin


class ProductSighting(Base, UUIDMixin, TimestampMixin):
    """某商品在某「逻辑日」的一次观测快照。"""
    __tablename__ = "product_sightings"

    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # 稳定指纹：xy:<source_id> / pdd:<sha1(clean_title)>
    item_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 逻辑日（东八 3 点日界），(item_key, seen_date) 唯一
    seen_date: Mapped[date] = mapped_column(Date, nullable=False)

    keyword: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 闲鱼=想要数 / PDD=销量
    heat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("item_key", "seen_date", name="uq_sightings_key_date"),
        Index("ix_sightings_key_date", "item_key", "seen_date"),
    )
