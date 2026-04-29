"""Trade history: orders, positions, pair-decisions

Stage 1 of trade history. Adds three tables:

- trade_orders: append-only log of every order our service sent to a venue.
  Sources Order History tab. Captures success and failure (raw exchange
  error / message / response).

- trade_positions: lifecycle of an open or closed position. May be a single
  leg or a pair (long/short or spot/short). Sources P&L tab in stage 2.

- trade_pair_decisions: persisted user choice about whether two single
  positions should be paired. Survives page refresh so we don't undo the
  user's manual Sync ⇆ / Unpair.

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-04-29
"""
import sqlalchemy as sa
from alembic import op


revision = 'd7e8f9a0b1c2'
down_revision = 'c6d7e8f9a0b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # trade_orders and trade_positions reference each other (a position
    # points to its open/close orders, an order points back to its
    # position), so we create both tables first without cross FKs and add
    # those constraints in a second pass.
    op.create_table(
        "trade_orders",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("wallet_id", sa.Integer, sa.ForeignKey("wallets.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("position_id", sa.Integer, nullable=True, index=True),  # FK added below
        sa.Column("exchange", sa.String, nullable=False, index=True),
        sa.Column("symbol", sa.String, nullable=False, index=True),
        sa.Column("side", sa.String, nullable=False),                # buy | sell
        sa.Column("intent", sa.String, nullable=False, index=True),  # open | close
        sa.Column("order_type", sa.String, nullable=False, default="market"),
        sa.Column("requested_qty", sa.Float, nullable=False),
        sa.Column("requested_price", sa.Float, nullable=True),
        sa.Column("leverage", sa.Integer, nullable=True),
        sa.Column("margin_mode", sa.String, nullable=True),
        sa.Column("status", sa.String, nullable=False, index=True),  # pending | filled | partial | failed | canceled
        sa.Column("exchange_order_id", sa.String, nullable=True, index=True),
        sa.Column("filled_qty", sa.Float, nullable=True),
        sa.Column("avg_fill_price", sa.Float, nullable=True),
        sa.Column("fee_usd", sa.Float, nullable=True),
        sa.Column("error_code", sa.String, nullable=True),
        sa.Column("error_message", sa.String, nullable=True),
        sa.Column("error_kind", sa.String, nullable=True),           # exchange | internal | user — drives UI sanitization
        sa.Column("raw_response", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, default=sa.func.now(), server_default=sa.func.now(), index=True),
        sa.Column("finalized_at", sa.DateTime, nullable=True),
    )

    op.create_table(
        "trade_positions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("kind", sa.String, nullable=False),              # single | pair
        sa.Column("pair_kind", sa.String, nullable=True),          # long_short | spot_short | null
        sa.Column("status", sa.String, nullable=False, index=True),  # open | closed
        sa.Column("symbol", sa.String, nullable=False, index=True),
        # leg A — always present
        sa.Column("leg_a_wallet_id", sa.Integer, sa.ForeignKey("wallets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("leg_a_exchange", sa.String, nullable=False),
        sa.Column("leg_a_side", sa.String, nullable=False),
        sa.Column("leg_a_qty", sa.Float, nullable=False),
        sa.Column("leg_a_entry_price", sa.Float, nullable=True),
        sa.Column("leg_a_exit_price", sa.Float, nullable=True),
        sa.Column("leg_a_realized_pnl_usd", sa.Float, nullable=True),
        sa.Column("leg_a_funding_pnl_usd", sa.Float, nullable=True),
        sa.Column("leg_a_fees_usd", sa.Float, nullable=True),
        sa.Column("leg_a_open_order_id", sa.Integer, nullable=True),   # FK added below
        sa.Column("leg_a_close_order_id", sa.Integer, nullable=True),  # FK added below
        # leg B — present only for pair
        sa.Column("leg_b_wallet_id", sa.Integer, sa.ForeignKey("wallets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("leg_b_exchange", sa.String, nullable=True),
        sa.Column("leg_b_side", sa.String, nullable=True),
        sa.Column("leg_b_qty", sa.Float, nullable=True),
        sa.Column("leg_b_entry_price", sa.Float, nullable=True),
        sa.Column("leg_b_exit_price", sa.Float, nullable=True),
        sa.Column("leg_b_realized_pnl_usd", sa.Float, nullable=True),
        sa.Column("leg_b_funding_pnl_usd", sa.Float, nullable=True),
        sa.Column("leg_b_fees_usd", sa.Float, nullable=True),
        sa.Column("leg_b_open_order_id", sa.Integer, nullable=True),   # FK added below
        sa.Column("leg_b_close_order_id", sa.Integer, nullable=True),  # FK added below
        # Aggregate
        sa.Column("realized_pnl_usd", sa.Float, nullable=True),
        sa.Column("entry_spread_pct", sa.Float, nullable=True),
        sa.Column("exit_spread_pct", sa.Float, nullable=True),
        sa.Column("opened_externally", sa.Boolean, nullable=False, default=False, server_default=sa.false()),
        sa.Column("closed_externally", sa.Boolean, nullable=False, default=False, server_default=sa.false()),
        sa.Column("opened_at", sa.DateTime, nullable=False, default=sa.func.now(), server_default=sa.func.now(), index=True),
        sa.Column("closed_at", sa.DateTime, nullable=True, index=True),
    )

    # Cross-reference FKs. SQLite doesn't support ALTER TABLE ADD CONSTRAINT
    # FOREIGN KEY at all (and doesn't enforce FKs by default anyway), so we
    # only add them on Postgres / other "real" backends. The columns still
    # work as plain integer references on SQLite.
    if op.get_bind().dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_trade_orders_position",
            "trade_orders", "trade_positions",
            ["position_id"], ["id"], ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_trade_positions_leg_a_open",
            "trade_positions", "trade_orders",
            ["leg_a_open_order_id"], ["id"], ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_trade_positions_leg_a_close",
            "trade_positions", "trade_orders",
            ["leg_a_close_order_id"], ["id"], ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_trade_positions_leg_b_open",
            "trade_positions", "trade_orders",
            ["leg_b_open_order_id"], ["id"], ondelete="SET NULL",
        )
        op.create_foreign_key(
            "fk_trade_positions_leg_b_close",
            "trade_positions", "trade_orders",
            ["leg_b_close_order_id"], ["id"], ondelete="SET NULL",
        )

    op.create_table(
        "trade_pair_decisions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("leg_a_key", sa.String, nullable=False),
        sa.Column("leg_b_key", sa.String, nullable=False),
        sa.Column("decision", sa.String, nullable=False),  # paired | unpaired
        sa.Column("created_at", sa.DateTime, nullable=False, default=sa.func.now(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, default=sa.func.now(), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.UniqueConstraint("user_id", "leg_a_key", "leg_b_key", name="uq_pair_decisions_user_legs"),
    )


def downgrade() -> None:
    op.drop_table("trade_pair_decisions")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_trade_positions_leg_b_close", "trade_positions", type_="foreignkey")
        op.drop_constraint("fk_trade_positions_leg_b_open", "trade_positions", type_="foreignkey")
        op.drop_constraint("fk_trade_positions_leg_a_close", "trade_positions", type_="foreignkey")
        op.drop_constraint("fk_trade_positions_leg_a_open", "trade_positions", type_="foreignkey")
        op.drop_constraint("fk_trade_orders_position", "trade_orders", type_="foreignkey")
    op.drop_table("trade_positions")
    op.drop_table("trade_orders")
