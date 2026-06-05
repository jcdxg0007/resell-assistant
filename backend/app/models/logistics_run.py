"""「查快递」拟人行为事件落库（roadmap §11.4）。

worker 在 burst 结尾(A) / inter-burst 静默期中段(B) 按概率去「我的订单→查看
物流」逛一下，每次执行后上报一行到这里，供「任务记录」抽屉合并展示。

为什么独立一张表（不复用 pdd_search_runs）：查快递不是"派发的采集任务"，没有
keyword/items/价格语义，混进 pdd_search_runs 会污染 PDD 看板/配额/已采集池统计
（那些默认全表是 PDD 采集）。单开一张极简表最干净，PDD/闲鱼侧零改动。

migration: logistics_run_01 (2026-06-05)
"""
from sqlalchemy import String, Integer, DateTime, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UUIDMixin, TimestampMixin


class LogisticsRun(Base, UUIDMixin, TimestampMixin):
    """一次「查快递」拟人动作的结果快照。

    created_at（TimestampMixin）≈ 动作发生落库时刻，任务记录时间轴用它。
    """
    __tablename__ = "logistics_runs"

    # 触发点：A = burst 结尾 / B = inter-burst 静默期中段
    trigger: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="A", index=True
    )
    # 结果：viewed = 有真实订单且点了查看物流 / empty = 订单页空 /
    #       nav_failed = 没导航到订单页（selector/页面异常）
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    account_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_serial: Mapped[str | None] = mapped_column(String(64), nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_logistics_runs_created", "created_at"),
        Index("ix_logistics_runs_status_created", "status", "created_at"),
    )
