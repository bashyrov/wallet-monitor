"""Trade fills backfill: trade_fills + fills_sync_cursor + leg market columns

Adds infrastructure for the PnL backfill feature:

- trade_fills: raw venue fills (per-execution rows). Per-(wallet, exchange,
  market, ext_trade_id) UNIQUE for idempotent re-syncs.

- fills_sync_cursor: per-(wallet, exchange, market) high-watermark for
  delta pulls — only fetch what's newer than `last_ts`.

- trade_positions.leg_a_market / leg_b_market: distinguish futures legs
  from spot legs. Default 'futures' so existing rows stay correct.

- trade_positions.source: where the row came from (platform | reconcile
  | fills_backfill). Lets the UI tag externally-reconstructed rows and
  helps the backfill skip rows it already created.

Revision ID: k0p1q2r3s4t5
Revises: j3k4l5m6n7o8
Create Date: 2026-05-09
"""
import sqlalchemy as sa
from alembic import op


revision = 'k0p1q2r3s4t5'
down_revision = 'j3k4l5m6n7o8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── trade_fills ───────────────────────────────────────────────────
    op.create_table(
        "trade_fills",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("wallet_id", sa.Integer,
                  sa.ForeignKey("wallets.id", ondelete="SET NULL"),
                  nullable=True, index=True),
        sa.Column("exchange", sa.String, nullable=False),
        sa.Column("market", sa.String, nullable=False),    # 'futures' | 'spot'
        # 'trade' = an actual fill, 'funding' = periodic funding settlement
        # (futures only). Storing them together so reconstruction can walk
        # one chronological stream and attribute funding to whatever position
        # was open at that timestamp.
        sa.Column("kind", sa.String, nullable=False, server_default="trade"),
        sa.Column("symbol", sa.String, nullable=False),
        sa.Column("side", sa.String, nullable=True),       # 'buy' | 'sell' | null for funding
        sa.Column("qty", sa.Float, nullable=False),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("fee_usd", sa.Float, nullable=True),
        sa.Column("realized_pnl_usd", sa.Float, nullable=True),
        sa.Column("ts", sa.DateTime, nullable=False),
        sa.Column("ext_trade_id", sa.String, nullable=False),
        sa.Column("ext_order_id", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint(
            "wallet_id", "exchange", "market", "kind", "ext_trade_id",
            name="uq_trade_fills_dedup",
        ),
    )
    op.create_index(
        "ix_trade_fills_user_ts",
        "trade_fills",
        ["user_id", "ts"],
        postgresql_using="btree",
    )
    op.create_index(
        "ix_trade_fills_user_ex_sym_market_ts",
        "trade_fills",
        ["user_id", "exchange", "symbol", "market", "ts"],
    )

    # ── fills_sync_cursor ─────────────────────────────────────────────
    op.create_table(
        "fills_sync_cursor",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("wallet_id", sa.Integer,
                  sa.ForeignKey("wallets.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("exchange", sa.String, nullable=False),
        sa.Column("market", sa.String, nullable=False),
        sa.Column("last_ts", sa.DateTime, nullable=True),
        sa.Column("last_synced_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint(
            "wallet_id", "exchange", "market",
            name="uq_fills_sync_cursor_key",
        ),
    )

    # ── trade_positions: leg_a/b market + source ──────────────────────
    op.add_column(
        "trade_positions",
        sa.Column("leg_a_market", sa.String, nullable=False,
                  server_default="futures"),
    )
    op.add_column(
        "trade_positions",
        sa.Column("leg_b_market", sa.String, nullable=True),
    )
    op.add_column(
        "trade_positions",
        sa.Column("source", sa.String, nullable=False,
                  server_default="platform"),
    )
    # Drop the server_default once existing rows are stamped — we want
    # the application code to decide explicitly going forward.
    op.alter_column("trade_positions", "leg_a_market", server_default=None)
    op.alter_column("trade_positions", "source", server_default=None)


def downgrade() -> None:
    op.drop_column("trade_positions", "source")
    op.drop_column("trade_positions", "leg_b_market")
    op.drop_column("trade_positions", "leg_a_market")
    op.drop_table("fills_sync_cursor")
    op.drop_index("ix_trade_fills_user_ex_sym_market_ts", table_name="trade_fills")
    op.drop_index("ix_trade_fills_user_ts", table_name="trade_fills")
    op.drop_table("trade_fills")
