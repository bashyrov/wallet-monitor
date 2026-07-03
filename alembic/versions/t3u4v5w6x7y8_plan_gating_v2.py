"""plans: max_spread_pct + allow_tp_sl_orders + repriced Screener $33 + unlim keys on paid

Feature-gate refresh for the paid tiers:
  · max_spread_pct       — cap on entry basis at open time (Free = 10.0,
                           Paid = 100.0 effectively unbounded). Enforced
                           in trade_service.place_open_order for both
                           market and limit orders.
  · allow_tp_sl_orders   — whether the user can place take-profit /
                           stop-loss orders. Free = False, Paid = True.
  · exchange_keys_per_venue — bumped to -1 (unlimited) on all paid tiers;
                           Free stays at 1 (was 1 already, no-op).
  · price_usd_monthly    — Screener drops $45 → $33; Full unchanged $55.

Revision ID: t3u4v5w6x7y8
Revises: s2t3u4v5w6x7
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa


revision = 't3u4v5w6x7y8'
down_revision = 's2t3u4v5w6x7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column('plans', sa.Column('max_spread_pct', sa.Numeric(6, 2),
                                     nullable=False, server_default='100.0'))
    op.add_column('plans', sa.Column('allow_tp_sl_orders', sa.Boolean(),
                                     nullable=False, server_default=sa.true()))

    # Free tier: cap at 10% entry spread, no TP/SL, 1 key per venue.
    bind.execute(sa.text("""
        UPDATE plans
           SET max_spread_pct = 10.0,
               allow_tp_sl_orders = false,
               exchange_keys_per_venue = 1
         WHERE slug = 'free'
    """))

    # Paid tiers: unbounded spread (100% is effectively no cap for arb),
    # TP/SL allowed, unlimited keys per venue.
    bind.execute(sa.text("""
        UPDATE plans
           SET max_spread_pct = 100.0,
               allow_tp_sl_orders = true,
               exchange_keys_per_venue = -1
         WHERE slug IN ('screener', 'full', 'unlim')
    """))

    # New Screener min price: $45 → $33. Full stays at $55.
    bind.execute(sa.text("UPDATE plans SET price_usd_monthly = 33 WHERE slug = 'screener'"))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE plans SET price_usd_monthly = 45 WHERE slug = 'screener'"))
    bind.execute(sa.text("""
        UPDATE plans
           SET exchange_keys_per_venue = 3
         WHERE slug IN ('screener', 'full')
    """))
    op.drop_column('plans', 'allow_tp_sl_orders')
    op.drop_column('plans', 'max_spread_pct')
