"""arb_positions + arb_trigger_orders + trade_positions.arb_position_id

Revision ID: i2c3d4e5f6g7
Revises: h1b2c3d4e5f6
Create Date: 2026-05-07

Storage for the unified Live Trading panel:

- `arb_positions` is the user-intent rollup ("the arb I want to track").
  One row wraps 1..N execution legs (`trade_positions.arb_position_id`).
- `arb_trigger_orders` is the server-side conditional-order ledger.
  Supports portion-based fills, infinite-fill, scheduled activation, and
  parent/child cascading (one parent open → linked TP + SL children).

Schema rationale: see DEV_PROMPT.md §7.0–7.2 + AUDIT_WALLETS.md.
"""
from alembic import op
import sqlalchemy as sa


revision = 'i2c3d4e5f6g7'
down_revision = 'h1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'arb_positions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),

        sa.Column('kind', sa.String(length=16), nullable=False),  # long_short | spot_short
        sa.Column('long_exchange', sa.String(length=32), nullable=False),
        sa.Column('long_symbol',   sa.String(length=64), nullable=False),
        sa.Column('long_wallet_id', sa.Integer(), sa.ForeignKey('wallets.id', ondelete='SET NULL'), nullable=True),
        sa.Column('short_exchange', sa.String(length=32), nullable=False),
        sa.Column('short_symbol',   sa.String(length=64), nullable=False),
        sa.Column('short_wallet_id', sa.Integer(), sa.ForeignKey('wallets.id', ondelete='SET NULL'), nullable=True),

        sa.Column('target_qty_token', sa.Float(), nullable=True),
        sa.Column('leverage', sa.Integer(), nullable=True),
        sa.Column('margin_mode', sa.String(length=8), nullable=False, server_default='isolated'),

        sa.Column('entry_spread_pct', sa.Float(), nullable=True),
        sa.Column('long_entry_price', sa.Float(), nullable=True),
        sa.Column('short_entry_price', sa.Float(), nullable=True),
        sa.Column('long_qty', sa.Float(), nullable=False, server_default='0'),
        sa.Column('short_qty', sa.Float(), nullable=False, server_default='0'),
        sa.Column('opened_at', sa.DateTime(), nullable=True),

        sa.Column('exit_spread_pct', sa.Float(), nullable=True),
        sa.Column('long_exit_price', sa.Float(), nullable=True),
        sa.Column('short_exit_price', sa.Float(), nullable=True),
        sa.Column('realized_pnl_usd', sa.Float(), nullable=True),
        sa.Column('closed_at', sa.DateTime(), nullable=True),

        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('synced_externally', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('closed_externally', sa.Boolean(), nullable=False, server_default=sa.false()),

        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
    )
    op.create_index('ix_arb_positions_user_status', 'arb_positions', ['user_id', 'status'])
    op.create_index('ix_arb_positions_opened', 'arb_positions', ['opened_at'])

    op.create_table(
        'arb_trigger_orders',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('arb_position_id', sa.Integer(), sa.ForeignKey('arb_positions.id', ondelete='CASCADE'), nullable=True),
        sa.Column('parent_trigger_id', sa.Integer(), sa.ForeignKey('arb_trigger_orders.id', ondelete='CASCADE'), nullable=True),

        sa.Column('kind', sa.String(length=16), nullable=False),  # open | close | tp | sl

        sa.Column('trigger_spread_pct', sa.Float(), nullable=True),  # null = "Last %" = market

        sa.Column('long_exchange', sa.String(length=32), nullable=True),
        sa.Column('long_symbol',   sa.String(length=64), nullable=True),
        sa.Column('long_wallet_id', sa.Integer(), sa.ForeignKey('wallets.id', ondelete='SET NULL'), nullable=True),
        sa.Column('short_exchange', sa.String(length=32), nullable=True),
        sa.Column('short_symbol',   sa.String(length=64), nullable=True),
        sa.Column('short_wallet_id', sa.Integer(), sa.ForeignKey('wallets.id', ondelete='SET NULL'), nullable=True),

        sa.Column('total_qty_token', sa.Float(), nullable=True),
        sa.Column('portion_size_token', sa.Float(), nullable=True),
        sa.Column('portions_filled', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('portions_target', sa.Integer(), nullable=True),
        sa.Column('infinite_fill', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('activate_at', sa.DateTime(), nullable=True),

        sa.Column('leverage', sa.Integer(), nullable=True),
        sa.Column('margin_mode', sa.String(length=8), nullable=False, server_default='isolated'),
        sa.Column('reduce_only', sa.Boolean(), nullable=False, server_default=sa.false()),

        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('last_fired_at', sa.DateTime(), nullable=True),
        sa.Column('error_kind', sa.String(length=16), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),

        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
    )
    op.create_index('ix_arb_trigger_orders_status', 'arb_trigger_orders', ['status'])
    op.create_index('ix_arb_trigger_orders_user', 'arb_trigger_orders', ['user_id'])
    op.create_index('ix_arb_trigger_orders_position', 'arb_trigger_orders', ['arb_position_id'])
    op.create_index('ix_arb_trigger_orders_parent', 'arb_trigger_orders', ['parent_trigger_id'])
    # Partial index: only rows that actually carry an activate_at timestamp.
    # SQLite supports partial indexes too (CREATE INDEX … WHERE …). Skip the
    # WHERE clause if the user's backend rejects it; on Postgres + SQLite it
    # works fine.
    op.execute(
        "CREATE INDEX ix_arb_trigger_orders_activate "
        "ON arb_trigger_orders(activate_at) "
        "WHERE activate_at IS NOT NULL"
    )

    op.add_column(
        'trade_positions',
        sa.Column(
            'arb_position_id', sa.Integer(),
            sa.ForeignKey('arb_positions.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )
    op.create_index('ix_trade_positions_arb', 'trade_positions', ['arb_position_id'])


def downgrade():
    op.drop_index('ix_trade_positions_arb', 'trade_positions')
    op.drop_column('trade_positions', 'arb_position_id')

    op.execute("DROP INDEX IF EXISTS ix_arb_trigger_orders_activate")
    op.drop_index('ix_arb_trigger_orders_parent', 'arb_trigger_orders')
    op.drop_index('ix_arb_trigger_orders_position', 'arb_trigger_orders')
    op.drop_index('ix_arb_trigger_orders_user', 'arb_trigger_orders')
    op.drop_index('ix_arb_trigger_orders_status', 'arb_trigger_orders')
    op.drop_table('arb_trigger_orders')

    op.drop_index('ix_arb_positions_opened', 'arb_positions')
    op.drop_index('ix_arb_positions_user_status', 'arb_positions')
    op.drop_table('arb_positions')
