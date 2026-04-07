"""add last_active_at to users

Revision ID: d0e1f2a3b4c5
Revises: e5f6a7b8c9d0
Create Date: 2026-04-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'd0e1f2a3b4c5'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('last_active_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('users', 'last_active_at')
