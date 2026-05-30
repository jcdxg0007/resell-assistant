"""selection_analysis: 十维度选品打分缓存表（按关键词一行）

「十维度选品」页 on-demand 打分的结果缓存。A/B/C 三层结果都以 JSON 落，
同词再打开秒出，「重新分析」覆盖。

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-05-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "selection_analysis",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("keyword", sa.String(length=128), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("xianyu_payload", sa.JSON(), nullable=True),
        sa.Column("pdd_payload", sa.JSON(), nullable=True),
        sa.Column("arbitrage", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_selection_analysis_keyword", "selection_analysis", ["keyword"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_selection_analysis_keyword", table_name="selection_analysis")
    op.drop_table("selection_analysis")
