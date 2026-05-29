"""pdd_search_runs: 加 items JSON 字段（逐条商品）

为支持「点关键词看采集到的商品」，把 worker 返回的商品列表落在 run 行上。
体量小（单次几十条 title/price/sales），直接 JSON 存，不另开表。

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa


revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pdd_search_runs", sa.Column("items", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("pdd_search_runs", "items")
