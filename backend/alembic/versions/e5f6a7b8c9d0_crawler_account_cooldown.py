"""add cooldown_until + last_used_at to accounts

Supports the crawler-pool rotation feature: selection_service picks
the least-recently-used crawler小号 whose cooldown_until is either
NULL or in the past. An ``empty_result`` risk signal from PDD/1688
(i.e. the platform flagged the account) bumps ``cooldown_until`` 60
minutes into the future, so the same burnt号 won't be picked again
until then.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-21
"""
from alembic import op
import sqlalchemy as sa


revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
    )
    # Index the column we filter+sort on during rotation (partial index
    # so we only pay the cost for crawler accounts).
    op.create_index(
        "ix_accounts_crawler_rotation",
        "accounts",
        ["platform", "last_used_at"],
        postgresql_where=sa.text("platform LIKE '%_crawler'"),
    )


def downgrade() -> None:
    op.drop_index("ix_accounts_crawler_rotation", table_name="accounts")
    op.drop_column("accounts", "cooldown_until")
    op.drop_column("accounts", "last_used_at")
