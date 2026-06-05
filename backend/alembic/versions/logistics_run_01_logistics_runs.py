"""logistics_runs: 「查快递」拟人行为事件落库

worker 在 burst 结尾(A)/静默期(B) 按概率查物流，每次执行上报一行，
「任务记录」抽屉与 pdd_search_runs / xianyu_search_runs 合并展示。
独立一张极简表，不污染 PDD/闲鱼采集统计。

Revision ID: logistics_run_01
Revises: xianyu_run_01
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "logistics_run_01"
down_revision = "xianyu_run_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "logistics_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("trigger", sa.String(8), nullable=False, server_default="A"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("account_name", sa.String(64), nullable=True),
        sa.Column("device_serial", sa.String(64), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_logistics_runs_trigger", "logistics_runs", ["trigger"])
    op.create_index("ix_logistics_runs_status", "logistics_runs", ["status"])
    op.create_index("ix_logistics_runs_created", "logistics_runs", ["created_at"])
    op.create_index("ix_logistics_runs_status_created", "logistics_runs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_logistics_runs_status_created", table_name="logistics_runs")
    op.drop_index("ix_logistics_runs_created", table_name="logistics_runs")
    op.drop_index("ix_logistics_runs_status", table_name="logistics_runs")
    op.drop_index("ix_logistics_runs_trigger", table_name="logistics_runs")
    op.drop_table("logistics_runs")
