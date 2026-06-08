"""PDD 商品级详情（按 goods_id 存「最新一次」深度收割结果）。

为什么单开一张表（见 docs/PDD-Step3-持久化与详情接入-设计.md，D1=C）：
店铺名/规格/品牌/口碑标签这类是**商品级、相对静态**的属性，天然按 goods_id 归一、
不随逻辑日重复。把它们从 product_sightings（管「每日价/热度时序」）里分出来单存，
既不让观测表长胖，又能按 goods_id 精确展示与归并。

来源：深度模式 dip 收割（worker `browse_detail_and_harvest` 的 out["fields"]
+ goods_id/thumb_url/detail_url）。只有真进过详情页的商品才有 goods_id，故才进此表；
list-level「路过卡」无 goods_id，仍只落 product_sightings。

upsert by goods_id：同一商品再次被 dip 到则刷新「最新」详情 + last_harvested_at。

migration: pdd_goods_01 (2026-06-08)
"""
from datetime import datetime

from sqlalchemy import String, Integer, Float, Text, JSON, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PddGoods(Base):
    """PDD 单个商品（goods_id）的最新详情快照。"""
    __tablename__ = "pdd_goods"

    # PDD 商品唯一标识（详情页 dumpsys 被动读取），主键
    goods_id: Mapped[str] = mapped_column(String(32), primary_key=True)

    shop_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    praise_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 结构化字段：榜单/口碑标签是 list，规格是 dict —— 统一用 JSON 列
    rank_badges: Mapped[list | None] = mapped_column(JSON, nullable=True)
    review_tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    specs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    discount: Mapped[float | None] = mapped_column(Float, nullable=True)
    thumb_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 收割时的标题/价格（便于无 sighting join 时直接展示，及核对同款）
    last_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    first_harvested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_harvested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )
