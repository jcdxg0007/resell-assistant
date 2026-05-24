"""PDD APP worker schema additions

Adds three columns needed by the self-built PDD app collector
(docs/PDD-自建采集-roadmap.md):

- ``accounts.bound_device_serial``: 1-机-1-号 strict binding. NULL means
  the account is not yet bound to any physical phone (e.g. legacy
  Playwright-era accounts, or quarantined numbers).
- ``products.seen_count``: incremented on every crawl that re-encounters
  the same goods_id. Reference signal for manual pin decisions; we
  deliberately do NOT auto-pin based on this (see §10.2).
- ``products.pinned_at``: NULL = candidate for daily cleanup (§10.3);
  non-NULL = user-pinned, never auto-cleaned. Pin is a fully manual
  operation, set/cleared from UI.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-05-24
"""
from alembic import op
import sqlalchemy as sa


revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("bound_device_serial", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_accounts_bound_device_serial",
        "accounts",
        ["bound_device_serial"],
    )
    op.add_column(
        "products",
        sa.Column("seen_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "products",
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_products_pinned_at",
        "products",
        ["pinned_at"],
        postgresql_where=sa.text("pinned_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_products_pinned_at", table_name="products")
    op.drop_column("products", "pinned_at")
    op.drop_column("products", "seen_count")
    op.drop_index("ix_accounts_bound_device_serial", table_name="accounts")
    op.drop_column("accounts", "bound_device_serial")
