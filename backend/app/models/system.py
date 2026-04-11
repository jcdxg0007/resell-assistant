from datetime import datetime

from sqlalchemy import String, Text, Integer, DateTime, JSON, Boolean, Float
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UUIDMixin, TimestampMixin


class User(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Account(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "accounts"

    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    account_name: Mapped[str] = mapped_column(String(128), nullable=False)
    identity_group: Mapped[str] = mapped_column(String(64), nullable=False)
    niche: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Browser context config
    proxy_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    viewport: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    fingerprint: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cookie_state_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    lifecycle_stage: Mapped[str] = mapped_column(String(32), nullable=False, default="nurturing")
    # "nurturing" / "cold_start" / "growing" / "mature" / "suspended"
    daily_publish_limit: Mapped[int] = mapped_column(Integer, default=2)
    daily_published_count: Mapped[int] = mapped_column(Integer, default=0)

    health_score: Mapped[float] = mapped_column(Float, default=100.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Session monitoring
    session_status: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    # "none" / "active" / "expired"
    session_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_expires_hint: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Task(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "tasks"

    task_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    # "pending" / "running" / "completed" / "failed" / "cancelled"
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)


class Notification(Base, UUIDMixin):
    __tablename__ = "notifications"

    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # "dingtalk" / "email"
    notification_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="sent")  # "sent" / "failed"
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class SystemConfig(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False, default="string")
    # "string" / "int" / "float" / "bool" / "json"
