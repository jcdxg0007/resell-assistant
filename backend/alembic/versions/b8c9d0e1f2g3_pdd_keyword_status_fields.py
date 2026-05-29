"""selection_keywords: PDD 调度专属字段

为支持 PDD 词库自动轮播（pdd_fire_from_lib.py），给 selection_keywords
表加 PDD 专属状态字段。现有的 last_crawled_at 是跨平台共享时间戳，
没法区分"PDD 跑过没"，所以必须有 PDD 自己的时间戳 + 状态。

加的列：
- pdd_last_searched_at  TIMESTAMPTZ  上次 PDD search 完成时间
- pdd_last_status       VARCHAR(16)  最近一次 PDD 结果（ok/empty/risk_blocked/failed）
- pdd_mode              VARCHAR(16)  默认 'fast'。fast / list_deep / detail_smart / detail_deep
- pdd_safe              BOOLEAN      默认 TRUE。FALSE 时即使 schedule_enabled=TRUE
                                     也会被 fire_from_lib 跳过（用于把"美瞳/医美"
                                     等明显敏感词永久禁用）
- pdd_searches_total    INTEGER      累计 PDD 搜过的次数（监控/调度均衡用）

Revision ID: b8c9d0e1f2g3
Revises: a7b8c9d0e1f2
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa


revision = "b8c9d0e1f2g3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "selection_keywords",
        sa.Column("pdd_last_searched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "selection_keywords",
        sa.Column("pdd_last_status", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "selection_keywords",
        sa.Column(
            "pdd_mode", sa.String(length=16),
            nullable=False, server_default="fast",
        ),
    )
    op.add_column(
        "selection_keywords",
        sa.Column(
            "pdd_safe", sa.Boolean(),
            nullable=False, server_default=sa.true(),
        ),
    )
    op.add_column(
        "selection_keywords",
        sa.Column(
            "pdd_searches_total", sa.Integer(),
            nullable=False, server_default="0",
        ),
    )
    # 调度时按 pdd_last_searched_at ASC NULLS FIRST 排序，加索引加速
    op.create_index(
        "ix_selection_keywords_pdd_sched",
        "selection_keywords",
        ["pdd_safe", "is_active", "schedule_enabled", "pdd_last_searched_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_selection_keywords_pdd_sched", table_name="selection_keywords"
    )
    op.drop_column("selection_keywords", "pdd_searches_total")
    op.drop_column("selection_keywords", "pdd_safe")
    op.drop_column("selection_keywords", "pdd_mode")
    op.drop_column("selection_keywords", "pdd_last_status")
    op.drop_column("selection_keywords", "pdd_last_searched_at")
