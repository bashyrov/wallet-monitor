"""screener alpha features: paper positions, opportunity snapshots, exchange health, anomaly alerts

Revision ID: i5j6k7l8m9n0
Revises: h4i5j6k7l8m9
Create Date: 2026-04-15 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'i5j6k7l8m9n0'
down_revision = 'h4i5j6k7l8m9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'paper_positions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('long_exchange', sa.String(), nullable=False),
        sa.Column('short_exchange', sa.String(), nullable=False),
        sa.Column('size_usd', sa.Float(), nullable=False),
        sa.Column('entry_long_price', sa.Float(), nullable=False),
        sa.Column('entry_short_price', sa.Float(), nullable=False),
        sa.Column('entry_spread_pct', sa.Float(), nullable=False),
        sa.Column('entry_fees_usd', sa.Float(), nullable=False, server_default='0'),
        sa.Column('accrued_funding_usd', sa.Float(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(), nullable=False, server_default='open'),  # open | closed
        sa.Column('opened_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('exit_spread_pct', sa.Float(), nullable=True),
        sa.Column('realized_pnl_usd', sa.Float(), nullable=True),
        sa.Column('last_updated', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        'opportunity_snapshots',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('symbol', sa.String(), nullable=False, index=True),
        sa.Column('long_exchange', sa.String(), nullable=False),
        sa.Column('short_exchange', sa.String(), nullable=False),
        sa.Column('gross_funding', sa.Float(), nullable=False),
        sa.Column('price_spread', sa.Float(), nullable=False),
        sa.Column('net_profit', sa.Float(), nullable=False),
        sa.Column('long_rate', sa.Float(), nullable=False),
        sa.Column('short_rate', sa.Float(), nullable=False),
        sa.Column('long_volume', sa.Float(), nullable=False, server_default='0'),
        sa.Column('short_volume', sa.Float(), nullable=False, server_default='0'),
        sa.Column('alpha_score', sa.Float(), nullable=True),
        sa.Column('snapshot_at', sa.DateTime(), nullable=False, server_default=sa.func.now(), index=True),
    )
    op.create_index('ix_opp_snap_pair_time', 'opportunity_snapshots',
                    ['symbol', 'long_exchange', 'short_exchange', 'snapshot_at'])

    op.create_table(
        'exchange_health',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('exchange', sa.String(), nullable=False, index=True),
        sa.Column('ts', sa.DateTime(), nullable=False, server_default=sa.func.now(), index=True),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False),
        sa.Column('error', sa.String(), nullable=True),
    )

    op.create_table(
        'anomaly_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=True, index=True),
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('long_exchange', sa.String(), nullable=False),
        sa.Column('short_exchange', sa.String(), nullable=False),
        sa.Column('spread_pct', sa.Float(), nullable=False),
        sa.Column('z_score', sa.Float(), nullable=False),
        sa.Column('mean_pct', sa.Float(), nullable=False),
        sa.Column('std_pct', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now(), index=True),
    )

    op.create_table(
        'watchlist_items',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('symbol', sa.String(), nullable=False),
        sa.Column('long_exchange', sa.String(), nullable=False),
        sa.Column('short_exchange', sa.String(), nullable=False),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table('watchlist_items')
    op.drop_table('anomaly_events')
    op.drop_table('exchange_health')
    op.drop_index('ix_opp_snap_pair_time', table_name='opportunity_snapshots')
    op.drop_table('opportunity_snapshots')
    op.drop_table('paper_positions')
