"""selection_keywords: 闲鱼调度字段（让闲鱼自动采集也走词库）

与 PDD 一组对称：xianyu_safe / xianyu_last_searched_at / xianyu_last_status /
xianyu_searches_total。xianyu_safe 默认 True（存量词默认参与闲鱼自动跑批）。

Revision ID: a1b2c3d4e5f6
Revises: f2a3b4c5d6e7
Create Date: 2026-05-30
"""
from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("selection_keywords", sa.Column("xianyu_safe", sa.Boolean(), server_default="true", nullable=False))
    op.add_column("selection_keywords", sa.Column("xianyu_last_searched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("selection_keywords", sa.Column("xianyu_last_status", sa.String(length=16), nullable=True))
    op.add_column("selection_keywords", sa.Column("xianyu_searches_total", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("selection_keywords", "xianyu_searches_total")
    op.drop_column("selection_keywords", "xianyu_last_status")
    op.drop_column("selection_keywords", "xianyu_last_searched_at")
    op.drop_column("selection_keywords", "xianyu_safe")
