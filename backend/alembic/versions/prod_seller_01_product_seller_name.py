"""products.seller_name: 卖家昵称（闲鱼列表页 userNickName）

比价页展示卖家用；旧行 NULL，重采后回填。

Revision ID: prod_seller_01
Revises: xianyu_kw_sched_01
Create Date: 2026-05-30
"""
from alembic import op
import sqlalchemy as sa


revision = "prod_seller_01"
down_revision = "xianyu_kw_sched_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("seller_name", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("products", "seller_name")
