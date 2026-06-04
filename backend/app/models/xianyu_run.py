"""闲鱼采集任务历史落库（与 pdd_search_runs 对称，但独立一张表）。

为什么不复用 pdd_search_runs：那张表及其上的看板/配额/已采集池逻辑全是
PDD 语义（console_data / summary / 每日配额计数都默认全表是 PDD）。把闲鱼行
混进去会污染这些统计。闲鱼搜不走手机 worker、字段也更少，单开一张表最干净，
PDD 侧零改动、零回归风险。

写入：instant_search（platform='xianyu'）跑完时调一次 persist_xianyu_run。
查询：「任务记录」抽屉把本表与 pdd_search_runs 按时间合并展示。

migration: xianyu_run_01 (2026-06-04)
"""
from sqlalchemy import String, Integer, DateTime, ForeignKey, JSON, Index, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UUIDMixin, TimestampMixin


class XianyuSearchRun(Base, UUIDMixin, TimestampMixin):
    """一次闲鱼 instant_search 的结果快照。

    created_at（TimestampMixin）≈ 任务完成落库时刻，任务记录时间轴用它。
    """
    __tablename__ = "xianyu_search_runs"

    # 来源：lib（自动跑批）/ batch（批量）/ manual（手动/前端）
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="manual", index=True
    )
    keyword_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("selection_keywords.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    keyword_text: Mapped[str] = mapped_column(String(128), nullable=False)
    category_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 结果状态：ok / empty / failed / risk_blocked
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    items_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    saved_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_signals: Mapped[list | None] = mapped_column(JSON, nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_xianyu_runs_created", "created_at"),
        Index("ix_xianyu_runs_status_created", "status", "created_at"),
    )
