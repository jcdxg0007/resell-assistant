"""pdd_category_account: 品类 ↔ PDD 采集号 多对多绑定

防双号画像趋同（roadmap §15）：一个品类分配给 1 个号=独占、多个号=共用。
未出现在本表的品类 = 未分配 = 不跑。account 指向 accounts(platform='pdd_crawler')。

Revision ID: pdd_cat_acct_01
Revises: prod_seller_01
Create Date: 2026-06-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "pdd_cat_acct_01"
down_revision = "prod_seller_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pdd_category_account",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "category_id", UUID(as_uuid=True),
            sa.ForeignKey("selection_categories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id", UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("category_id", "account_id", name="uq_pdd_cat_account"),
    )
    op.create_index("ix_pdd_category_account_category_id", "pdd_category_account", ["category_id"])
    op.create_index("ix_pdd_category_account_account_id", "pdd_category_account", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_pdd_category_account_account_id", table_name="pdd_category_account")
    op.drop_index("ix_pdd_category_account_category_id", table_name="pdd_category_account")
    op.drop_table("pdd_category_account")
