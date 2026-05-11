"""add uncategorized seed category

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-02
"""
from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Container for keywords submitted via the free-form search entry point.
    # instant_search will auto-create a Keyword under this category when the
    # user searches for a term that's not yet in the curated library.
    op.execute(
        sa.text(
            "INSERT INTO selection_categories "
            "(id, name, slug, niche_hint, display_order, is_active, "
            " created_at, updated_at) "
            "VALUES (gen_random_uuid(), '未分类', 'uncategorized', "
            "        '自由搜索自动归入，后续人工整理', 999, true, "
            "        now(), now()) "
            "ON CONFLICT (slug) DO NOTHING"
        )
    )


def downgrade() -> None:
    # Cascade deletes any keyword auto-created under the uncategorized bucket.
    op.execute(
        sa.text("DELETE FROM selection_categories WHERE slug = 'uncategorized'")
    )
