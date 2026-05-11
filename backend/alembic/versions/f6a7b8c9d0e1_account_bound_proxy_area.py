"""add bound_proxy_area to accounts

Geographic stickiness for crawler accounts. Each crawler account is
pinned to a single 青果 `area` (省级区划码), so the platform sees "the
same user coming back from the same province" instead of "this account
is logging in from all over China" — the single most-obvious机器人信号
short of leaking webdriver=true.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-04-22
"""
from alembic import op
import sqlalchemy as sa


revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("bound_proxy_area", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "bound_proxy_area")
