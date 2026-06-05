"""pdd_pins: PDD 端「快照收藏」冻结落库

十维选品 PDD 列是采集快照（无稳定 product_id/无链接/每日清库覆盖），无法像闲鱼那样
给 products 打 pinned_at。收藏时把快照整体冻结存这张表，跨日保留。

Revision ID: pdd_pin_01
Revises: logistics_run_01
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "pdd_pin_01"
down_revision = "logistics_run_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pdd_pins",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("fingerprint", sa.String(40), nullable=False),
        sa.Column("keyword", sa.String(128), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("sales", sa.Integer(), nullable=True),
        sa.Column("badges", sa.JSON(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_pdd_pins_fingerprint", "pdd_pins", ["fingerprint"], unique=True)
    op.create_index("ix_pdd_pins_pinned_at", "pdd_pins", ["pinned_at"])


def downgrade() -> None:
    op.drop_index("ix_pdd_pins_pinned_at", table_name="pdd_pins")
    op.drop_index("ix_pdd_pins_fingerprint", table_name="pdd_pins")
    op.drop_table("pdd_pins")
