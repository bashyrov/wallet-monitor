"""add_is_admin_to_users

Revision ID: fb0ca8a11562
Revises: 014613d42a04
Create Date: 2026-04-05 18:27:20.853923

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fb0ca8a11562'
down_revision: Union[str, Sequence[str], None] = '014613d42a04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('is_admin', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Auto-promote the first registered user (lowest id) to admin
    op.execute("UPDATE users SET is_admin = true WHERE id = (SELECT MIN(id) FROM users)")


def downgrade() -> None:
    op.drop_column('users', 'is_admin')
