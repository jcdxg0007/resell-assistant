"""pdd_goods 表 + product_sightings 加深度收割字段（Step 3 批 1）

见 docs/PDD-Step3-持久化与详情接入-设计.md：
- 新建 pdd_goods（商品级最新详情，按 goods_id upsert）
- product_sightings 加 goods_id / sold_count / coupon_price 三列

纯加列/加表，无数据依赖；worker 未回传前新字段全 NULL，无副作用。

Revision ID: pdd_goods_01
Revises: product_sighting_01
Create Date: 2026-06-08
"""
from alembic import op
import sqlalchemy as sa


revision = "pdd_goods_01"
down_revision = "product_sighting_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── product_sightings 加三列（时序信号 / 附加身份）
    op.add_column("product_sightings", sa.Column("goods_id", sa.String(32), nullable=True))
    op.add_column("product_sightings", sa.Column("sold_count", sa.Integer(), nullable=True))
    op.add_column("product_sightings", sa.Column("coupon_price", sa.Float(), nullable=True))
    op.create_index(
        "ix_product_sightings_goods_id", "product_sightings", ["goods_id"]
    )

    # ── pdd_goods（商品级静态详情，按 goods_id 主键）
    op.create_table(
        "pdd_goods",
        sa.Column("goods_id", sa.String(32), primary_key=True),
        sa.Column("shop_name", sa.String(128), nullable=True),
        sa.Column("comment_count", sa.Integer(), nullable=True),
        sa.Column("praise_rate", sa.Float(), nullable=True),
        sa.Column("rank_badges", sa.JSON(), nullable=True),
        sa.Column("review_tags", sa.JSON(), nullable=True),
        sa.Column("specs", sa.JSON(), nullable=True),
        sa.Column("discount", sa.Float(), nullable=True),
        sa.Column("thumb_url", sa.Text(), nullable=True),
        sa.Column("detail_url", sa.Text(), nullable=True),
        sa.Column("last_title", sa.Text(), nullable=True),
        sa.Column("last_price", sa.Float(), nullable=True),
        sa.Column(
            "first_harvested_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "last_harvested_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("pdd_goods")
    op.drop_index("ix_product_sightings_goods_id", table_name="product_sightings")
    op.drop_column("product_sightings", "coupon_price")
    op.drop_column("product_sightings", "sold_count")
    op.drop_column("product_sightings", "goods_id")
