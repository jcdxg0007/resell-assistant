"""pdd_search_runs: PDD 采集任务历史落库

每跑完一个 PDD search 任务就落一行，作为 Ops 看板 / 复盘 / 告警的地基。
Redis 结果是短 TTL 传输态，这张表是长期可查询的历史态。

建表 pdd_search_runs，字段都是结果发生那刻已有的东西（零额外查询）。

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2g3
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2g3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pdd_search_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="lib"),
        sa.Column("keyword_id", UUID(as_uuid=True), nullable=True),
        sa.Column("keyword_text", sa.String(length=128), nullable=False),
        sa.Column("category_name", sa.String(length=64), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("items_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("price_min", sa.Float(), nullable=True),
        sa.Column("price_median", sa.Float(), nullable=True),
        sa.Column("risk_signals", sa.JSON(), nullable=True),
        sa.Column("device_serial", sa.String(length=64), nullable=True),
        sa.Column("account_name", sa.String(length=64), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["keyword_id"], ["selection_keywords.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_pdd_search_runs_task_id", "pdd_search_runs", ["task_id"])
    op.create_index("ix_pdd_search_runs_source", "pdd_search_runs", ["source"])
    op.create_index("ix_pdd_search_runs_keyword_id", "pdd_search_runs", ["keyword_id"])
    op.create_index("ix_pdd_search_runs_status", "pdd_search_runs", ["status"])
    op.create_index("ix_pdd_runs_created", "pdd_search_runs", ["created_at"])
    op.create_index(
        "ix_pdd_runs_status_created", "pdd_search_runs", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_pdd_runs_status_created", table_name="pdd_search_runs")
    op.drop_index("ix_pdd_runs_created", table_name="pdd_search_runs")
    op.drop_index("ix_pdd_search_runs_status", table_name="pdd_search_runs")
    op.drop_index("ix_pdd_search_runs_keyword_id", table_name="pdd_search_runs")
    op.drop_index("ix_pdd_search_runs_source", table_name="pdd_search_runs")
    op.drop_index("ix_pdd_search_runs_task_id", table_name="pdd_search_runs")
    op.drop_table("pdd_search_runs")
