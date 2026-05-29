"""PDD 采集任务历史落库（Ops 看板 / 复盘 / 告警的地基）。

每跑完一个 PDD search 任务（无论来自词库轮播 pdd_fire_from_lib、选品打分
celery 流程，还是手动派发），都往 pdd_search_runs 写一行。Redis 里的结果
是短 TTL（10min）的"传输态"，这张表才是长期可查询的"历史态"。

用途：
- Ops 看板：今日跑了多少、成功率、风控次数、近 7 天趋势、最近任务流水
- 复盘：某个词/品类长期表现，哪些词总是 empty（冷门可下架）
- 告警：连续 risk_blocked / failed 时能被发现

字段都是"结果发生那一刻手上已有的东西"，不额外查询，写入成本极低。

migration: c9d0e1f2a3b4 (2026-05-29)
"""
from datetime import datetime

from sqlalchemy import (
    String, Integer, Float, DateTime, ForeignKey, JSON, Index, Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UUIDMixin, TimestampMixin


class PddSearchRun(Base, UUIDMixin, TimestampMixin):
    """一次 PDD search 任务的结果快照。

    created_at（来自 TimestampMixin）≈ 任务完成落库时间，Ops 看板的时间轴
    就用它。keyword_id 用 SET NULL：词被删了历史还在（审计需要）。
    """
    __tablename__ = "pdd_search_runs"

    # worker 侧任务 id（PddAppTask.task_id），便于和 worker 日志对账
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # 来源：lib（词库轮播）/ selection（选品打分流程）/ manual（手动派）/ emergency
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="lib", index=True
    )

    # 关键词。keyword_id 可空（celery 选品流程按词文本搜，未必来自词库）；
    # keyword_text 始终冗余存一份，方便词被删后历史仍可读。
    keyword_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("selection_keywords.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    keyword_text: Mapped[str] = mapped_column(String(128), nullable=False)
    category_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # worker 跑的模式：fast / deep
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 结果状态：ok / empty / partial / failed / risk_blocked / timeout
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    items_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    price_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_median: Mapped[float | None] = mapped_column(Float, nullable=True)

    risk_signals: Mapped[list | None] = mapped_column(JSON, nullable=True)
    device_serial: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # 看板主查询：按时间倒序 + 按状态过滤
        Index("ix_pdd_runs_created", "created_at"),
        Index("ix_pdd_runs_status_created", "status", "created_at"),
    )
