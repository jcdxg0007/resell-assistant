"""add_cookies_data_to_accounts

Revision ID: a1b2c3d4e5f6
Revises: 340599339e70
Create Date: 2026-04-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '340599339e70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('accounts', sa.Column('cookies_data', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('accounts', 'cookies_data')
