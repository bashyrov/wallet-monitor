"""add is_blocked and request_count to users

Revision ID: c3d4e5f6a7b8
Revises: fb0ca8a11562
Create Date: 2026-04-06 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f6a7b8'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('is_blocked',     sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('users', sa.Column('request_count',  sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('users', 'request_count')
    op.drop_column('users', 'is_blocked')
