"""selection module: category/keyword/keyword_products/keyword_scores

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-02
"""
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. selection_categories -----------------------------------------------
    op.create_table(
        "selection_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("niche_hint", sa.String(256), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_selection_categories_slug", "selection_categories",
        ["slug"], unique=True,
    )

    # 2. selection_keywords -------------------------------------------------
    op.create_table(
        "selection_keywords",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.String(128), nullable=False),
        sa.Column("target_platforms", sa.JSON(), nullable=True),
        sa.Column("max_items_per_platform", sa.Integer(), nullable=False,
                  server_default="90"),
        sa.Column("schedule_enabled", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("last_crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_id"], ["selection_categories.id"], ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_selection_keywords_category_id", "selection_keywords",
        ["category_id"],
    )
    op.create_unique_constraint(
        "uq_keyword_category_text", "selection_keywords",
        ["category_id", "text"],
    )

    # 3. keyword_products (many-to-many) -----------------------------------
    op.create_table(
        "keyword_products",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("keyword_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_rank_in_search", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["keyword_id"], ["selection_keywords.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_keyword_products_keyword_id", "keyword_products", ["keyword_id"],
    )
    op.create_index(
        "ix_keyword_products_product_id", "keyword_products", ["product_id"],
    )
    op.create_unique_constraint(
        "uq_kw_product", "keyword_products", ["keyword_id", "product_id"],
    )

    # 4. keyword_scores ----------------------------------------------------
    op.create_table(
        "keyword_scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("keyword_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("dimension_scores", sa.JSON(), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["keyword_id"], ["selection_keywords.id"], ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_keyword_scores_keyword_id", "keyword_scores", ["keyword_id"],
    )
    op.create_index(
        "ix_keyword_scores_kw_time", "keyword_scores",
        ["keyword_id", "scored_at"],
    )

    # 5. score_type semantic migration -------------------------------------
    op.execute(
        "UPDATE product_scores SET score_type = 'product_10d' "
        "WHERE score_type = 'xianyu_10d'"
    )

    # 6. seed data ---------------------------------------------------------
    conn = op.get_bind()
    cat_id = str(uuid.uuid4())
    conn.execute(
        sa.text(
            "INSERT INTO selection_categories "
            "(id, name, slug, niche_hint, display_order, is_active, "
            " created_at, updated_at) "
            "VALUES (:id, :name, :slug, :hint, 0, true, now(), now())"
        ),
        {
            "id": cat_id,
            "name": "相机配件",
            "slug": "camera-accessories",
            "hint": "GoPro / DJI / Insta360 等运动相机周边",
        },
    )
    for kw_text in ["action4", "运动相机自拍杆", "迷你补光灯"]:
        conn.execute(
            sa.text(
                "INSERT INTO selection_keywords "
                "(id, category_id, text, target_platforms, "
                " max_items_per_platform, schedule_enabled, is_active, "
                " created_at, updated_at) "
                "VALUES (:id, :cat_id, :text, "
                " CAST(:platforms AS JSON), 90, true, true, now(), now())"
            ),
            {
                "id": str(uuid.uuid4()),
                "cat_id": cat_id,
                "text": kw_text,
                "platforms": '["xianyu","taobao","pdd","xiaohongshu"]',
            },
        )


def downgrade() -> None:
    op.execute(
        "UPDATE product_scores SET score_type = 'xianyu_10d' "
        "WHERE score_type = 'product_10d'"
    )
    op.drop_index("ix_keyword_scores_kw_time", table_name="keyword_scores")
    op.drop_index("ix_keyword_scores_keyword_id", table_name="keyword_scores")
    op.drop_table("keyword_scores")

    op.drop_index("ix_keyword_products_product_id", table_name="keyword_products")
    op.drop_index("ix_keyword_products_keyword_id", table_name="keyword_products")
    op.drop_table("keyword_products")

    op.drop_index("ix_selection_keywords_category_id", table_name="selection_keywords")
    op.drop_table("selection_keywords")

    op.drop_index("ix_selection_categories_slug", table_name="selection_categories")
    op.drop_table("selection_categories")
