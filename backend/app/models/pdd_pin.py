"""PDD 端「快照收藏」落库。

十维选品 PDD 那一列来自 PddSearchRun.items 的采集快照：没有稳定 product_id、
没有可跳转链接、每日清库会被新一次采集覆盖，所以没法像闲鱼那样给 Product 行打
pinned_at。这里把用户收藏的那条快照整体冻结存下来，跨日永久保留。

幂等/去重：fingerprint = sha1(keyword|title)[:32]，同词同标题再点收藏只刷新快照
（价格/销量/标签/图），不重复建行。

为什么独立一张表（不复用 products.pinned_at）：PDD 快照不是真实商品行，混进
products 会污染闲鱼采集池/打分/清库统计。单开极简表最干净。

migration: pdd_pin_01 (2026-06-05)
"""
from datetime import datetime

from sqlalchemy import String, Integer, Float, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UUIDMixin, TimestampMixin


class PddPin(Base, UUIDMixin, TimestampMixin):
    """一条 PDD 采集快照的冻结收藏。"""
    __tablename__ = "pdd_pins"

    # sha1(keyword|title)[:32]，唯一，收藏/取消/去重都靠它
    fingerprint: Mapped[str] = mapped_column(
        String(40), nullable=False, unique=True, index=True
    )
    keyword: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sales: Mapped[int | None] = mapped_column(Integer, nullable=True)
    badges: Mapped[list | None] = mapped_column(JSON, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pinned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
