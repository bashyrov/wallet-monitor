"""add balance_history table

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-04-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'f2a3b4c5d6e7'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'balance_history',
        sa.Column('id',          sa.Integer(),  primary_key=True),
        sa.Column('user_id',     sa.Integer(),  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('usd_total',   sa.Float(),    nullable=False),
        sa.Column('snapshot_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_balance_history_user_id',     'balance_history', ['user_id'])
    op.create_index('ix_balance_history_snapshot_at', 'balance_history', ['snapshot_at'])


def downgrade():
    op.drop_index('ix_balance_history_snapshot_at', 'balance_history')
    op.drop_index('ix_balance_history_user_id',     'balance_history')
    op.drop_table('balance_history')
