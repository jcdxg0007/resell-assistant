"""product_sightings: 跨天同款观测（Phase 1，精确指纹 L1）

用稳定指纹 item_key 把同一挂牌/快照在不同逻辑日的出现各记一条，
支撑选品页「持续在售」标签与价格/热度趋势。

Revision ID: product_sighting_01
Revises: pdd_pin_01
Create Date: 2026-06-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "product_sighting_01"
down_revision = "pdd_pin_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_sightings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("item_key", sa.String(64), nullable=False),
        sa.Column("seen_date", sa.Date(), nullable=False),
        sa.Column("keyword", sa.String(128), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("heat", sa.Integer(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_product_sightings_platform", "product_sightings", ["platform"])
    op.create_index("ix_product_sightings_item_key", "product_sightings", ["item_key"])
    op.create_index("ix_sightings_key_date", "product_sightings", ["item_key", "seen_date"])
    op.create_unique_constraint("uq_sightings_key_date", "product_sightings", ["item_key", "seen_date"])


def downgrade() -> None:
    op.drop_constraint("uq_sightings_key_date", "product_sightings", type_="unique")
    op.drop_index("ix_sightings_key_date", table_name="product_sightings")
    op.drop_index("ix_product_sightings_item_key", table_name="product_sightings")
    op.drop_index("ix_product_sightings_platform", table_name="product_sightings")
    op.drop_table("product_sightings")
