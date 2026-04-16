"""add can_trade flag to wallets

Revision ID: k7l8m9n0o1p2
Revises: j6k7l8m9n0o1
Create Date: 2026-04-16 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'k7l8m9n0o1p2'
down_revision = 'j6k7l8m9n0o1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('wallets', sa.Column('can_trade', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('wallets', 'can_trade')
