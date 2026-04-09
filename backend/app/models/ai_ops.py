from datetime import datetime

from sqlalchemy import String, Text, DateTime, JSON, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UUIDMixin, TimestampMixin


class DailyReport(Base, UUIDMixin):
    __tablename__ = "daily_reports"

    report_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, unique=True, index=True)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False)
    # {"xianyu": {"orders": 8, "revenue": 1240, "profit": 680}, "xhs": {...}, ...}
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False)
    # {"xianyu_daily_profit_trend": ..., "xhs_interaction_rate": ..., ...}
    suggestions: Mapped[list] = mapped_column(JSON, nullable=False)
    report_text: Mapped[str] = mapped_column(Text, nullable=False)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DailyCheck(Base, UUIDMixin):
    __tablename__ = "daily_checks"

    check_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    check_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # "account_health" / "product_status" / "crawl_integrity" / "competition_change"
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # "ok" / "warning" / "error"
    details: Mapped[dict] = mapped_column(JSON, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class AiSuggestion(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "ai_suggestions"

    suggestion_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # "reprice" / "restock" / "delist" / "content" / "risk_control" / "selection"
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    action_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    # "pending" / "approved" / "executed" / "ignored" / "auto_executed"
    auto_executable: Mapped[bool] = mapped_column(Boolean, default=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
