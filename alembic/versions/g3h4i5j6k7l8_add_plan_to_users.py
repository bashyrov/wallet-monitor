"""add plan and plan_expires_at to users

Revision ID: g3h4i5j6k7l8
Revises: f2a3b4c5d6e7
Create Date: 2026-04-10 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'g3h4i5j6k7l8'
down_revision = 'a2b3c4d5e6f7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('plan', sa.String(), nullable=False, server_default='basic'))
    op.add_column('users', sa.Column('plan_expires_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('users', 'plan_expires_at')
    op.drop_column('users', 'plan')
