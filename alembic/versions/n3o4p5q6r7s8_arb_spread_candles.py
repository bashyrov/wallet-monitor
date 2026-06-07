"""arb spread candles — 5s/1m/1h time-series tables for in/out spread history

Revision ID: n3o4p5q6r7s8
Revises: m2n3o4p5q6r7
Create Date: 2026-06-07

Three tables for the new in/out spread chart history pipeline. The
go-fetcher writes 5s candles via Redis stream → Python consumer batch
inserts → cron rollups produce 1m and 1h tiers.

Why three tables (not one with `tf` column):
  - Retention is per-tier: 24h/7d/90d. Per-table DELETE is simpler than
    `DELETE ... WHERE tf='5s' AND bucket_ts < ...` and lets us re-cluster
    each table on its own write pattern.
  - Read queries are tighter — one PK index probe, no WHERE tf= clause
    that the planner has to refine.

Per-row size ~80B + ~50B PK index ≈ 130B. Projected 7-day steady-state
volume with top-500 active pairs:
  - 5s × 24h:  500 × 17,280 = 8.6M rows  ≈ 1.1 GB
  - 1m × 7d:   500 × 10,080 = 5.0M rows  ≈ 0.6 GB
  - 1h × 90d:  500 × 2,160  = 1.1M rows  ≈ 0.1 GB
  - Total ~15M rows / ~2 GB

Known gap: go-fetcher restart loses the in-memory bucket aggregator's
current 5s window (≤5s of data). ON CONFLICT recovery covers crash-
during-flush; it does NOT reconstruct the in-flight bucket. Chart will
show a whitespace gap there (rendered with `whitespace_data` in
lightweight-charts) — honest gap > fake continuous line.
"""
from alembic import op
import sqlalchemy as sa


revision = 'n3o4p5q6r7s8'
down_revision = 'm2n3o4p5q6r7'
branch_labels = None
depends_on = None


_OHLC_COLS = [
    sa.Column('in_open',  sa.Float, nullable=False),
    sa.Column('in_high',  sa.Float, nullable=False),
    sa.Column('in_low',   sa.Float, nullable=False),
    sa.Column('in_close', sa.Float, nullable=False),
    sa.Column('out_open',  sa.Float, nullable=False),
    sa.Column('out_high',  sa.Float, nullable=False),
    sa.Column('out_low',   sa.Float, nullable=False),
    sa.Column('out_close', sa.Float, nullable=False),
    sa.Column('samples',  sa.SmallInteger, nullable=False, server_default='1'),
]


def _create_candle_table(name: str) -> None:
    op.create_table(
        name,
        sa.Column('exchange_long',  sa.String(16), nullable=False),
        sa.Column('exchange_short', sa.String(16), nullable=False),
        sa.Column('symbol',         sa.String(32), nullable=False),
        sa.Column('bucket_ts',      sa.BigInteger, nullable=False),
        *_OHLC_COLS,
        sa.PrimaryKeyConstraint(
            'exchange_long', 'exchange_short', 'symbol', 'bucket_ts',
            name=f'pk_{name}',
        ),
    )
    # Read-path index: chart query is always "this (symbol, long, short)
    # over a time window, descending so the most recent candle is first."
    # Maps 1:1 to /api/screener/arb-spread-history's SELECT pattern.
    op.create_index(
        f'idx_{name}_lookup', name,
        ['symbol', 'exchange_long', 'exchange_short', 'bucket_ts'],
        postgresql_using='btree',
    )


def upgrade() -> None:
    _create_candle_table('arb_spread_candles_5s')
    _create_candle_table('arb_spread_candles_1m')
    _create_candle_table('arb_spread_candles_1h')


def downgrade() -> None:
    op.drop_index('idx_arb_spread_candles_1h_lookup', 'arb_spread_candles_1h')
    op.drop_table('arb_spread_candles_1h')
    op.drop_index('idx_arb_spread_candles_1m_lookup', 'arb_spread_candles_1m')
    op.drop_table('arb_spread_candles_1m')
    op.drop_index('idx_arb_spread_candles_5s_lookup', 'arb_spread_candles_5s')
    op.drop_table('arb_spread_candles_5s')
