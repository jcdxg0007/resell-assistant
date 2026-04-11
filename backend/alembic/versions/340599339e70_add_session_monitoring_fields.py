"""add_session_monitoring_fields

Revision ID: 340599339e70
Revises: 4a0674d8a46c
Create Date: 2026-04-11 10:33:04.304396

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '340599339e70'
down_revision: Union[str, Sequence[str], None] = '4a0674d8a46c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('accounts', sa.Column('session_status', sa.String(length=16), server_default='none', nullable=False))
    op.add_column('accounts', sa.Column('session_checked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('accounts', sa.Column('session_expires_hint', sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column('accounts', 'session_expires_hint')
    op.drop_column('accounts', 'session_checked_at')
    op.drop_column('accounts', 'session_status')
