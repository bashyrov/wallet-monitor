"""add balance_snapshots table

Revision ID: e5f6a7b8c9d0
Revises: c3d4e5f6a7b8
Create Date: 2026-04-06 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5f6a7b8c9d0'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'balance_snapshots',
        sa.Column('id',           sa.Integer(),  nullable=False),
        sa.Column('wallet_id',    sa.Integer(),  nullable=False),
        sa.Column('user_id',      sa.Integer(),  nullable=False),
        sa.Column('totals',       sa.JSON(),     nullable=False),
        sa.Column('stable_total', sa.Float(),    nullable=False, server_default='0'),
        sa.Column('snapshot_at',  sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['wallet_id'], ['wallets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'],   ['users.id'],   ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('wallet_id'),
    )
    op.create_index('ix_balance_snapshots_id',        'balance_snapshots', ['id'],        unique=False)
    op.create_index('ix_balance_snapshots_wallet_id', 'balance_snapshots', ['wallet_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_balance_snapshots_wallet_id', table_name='balance_snapshots')
    op.drop_index('ix_balance_snapshots_id',        table_name='balance_snapshots')
    op.drop_table('balance_snapshots')
