"""products.published_at: 平台挂牌发布时间（「挂牌新鲜度」维度用）

闲鱼列表页带 publish_time_ms，落库时转存到此列；旧行 NULL，重采后回填。
打分侧按无数据优雅降级，不影响存量数据。

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-05-30
"""
from alembic import op
import sqlalchemy as sa


revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("products", "published_at")
