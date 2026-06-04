"""xianyu_search_runs: 闲鱼采集任务历史落库

与 pdd_search_runs 对称、独立一张表（不污染 PDD 看板/配额统计）。
instant_search(platform='xianyu') 跑完写一行，「任务记录」抽屉合并展示。

Revision ID: xianyu_run_01
Revises: pdd_cat_acct_01
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "xianyu_run_01"
down_revision = "pdd_cat_acct_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "xianyu_search_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column(
            "keyword_id", UUID(as_uuid=True),
            sa.ForeignKey("selection_keywords.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("keyword_text", sa.String(128), nullable=False),
        sa.Column("category_name", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("items_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("saved_count", sa.Integer(), nullable=True),
        sa.Column("risk_signals", sa.JSON(), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_xianyu_search_runs_source", "xianyu_search_runs", ["source"])
    op.create_index("ix_xianyu_search_runs_keyword_id", "xianyu_search_runs", ["keyword_id"])
    op.create_index("ix_xianyu_search_runs_status", "xianyu_search_runs", ["status"])
    op.create_index("ix_xianyu_runs_created", "xianyu_search_runs", ["created_at"])
    op.create_index("ix_xianyu_runs_status_created", "xianyu_search_runs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_xianyu_runs_status_created", table_name="xianyu_search_runs")
    op.drop_index("ix_xianyu_runs_created", table_name="xianyu_search_runs")
    op.drop_index("ix_xianyu_search_runs_status", table_name="xianyu_search_runs")
    op.drop_index("ix_xianyu_search_runs_keyword_id", table_name="xianyu_search_runs")
    op.drop_index("ix_xianyu_search_runs_source", table_name="xianyu_search_runs")
    op.drop_table("xianyu_search_runs")
