"""perpdex wallets: backfill purpose from 'portfolio' to 'both'

Revision ID: h1b2c3d4e5f6
Revises: g0a1b2c3d4e5
Create Date: 2026-05-07

Background:
- Perp-DEX wallets (Aster, Hyperliquid, Paradex, Ethereal, Lighter) are a
  single private-key/credential identity that serves both viewing AND
  trading by design — there is no separate "read-only key" like on CEX.
- wallet_service.create_wallet historically forced perpdex purpose to
  'portfolio' regardless of body input. Combined with the trade-service
  filter `purpose IN ('screener','both')`, this meant perpdex wallets
  were silently excluded from /api/trade/positions, balances, and the
  spot-short auto-detection — a long-standing data-truth bug.
- The code is now fixed to default new perpdex wallets to 'both' and to
  honor PATCH purpose updates. This migration backfills existing rows.
- Wallets explicitly set to 'screener' or 'both' are left as-is. Only
  rows still on the legacy 'portfolio' default are upgraded.

Rollback flips matching rows back to 'portfolio'.
"""
from alembic import op


revision = 'h1b2c3d4e5f6'
down_revision = 'g0a1b2c3d4e5'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "UPDATE wallets "
        "SET purpose = 'both', can_trade = TRUE "
        "WHERE wallet_type = 'perpdex' AND purpose = 'portfolio'"
    )


def downgrade():
    # Revert only rows that look like they were touched by this migration.
    # If the user explicitly downgraded a perpdex wallet via PATCH after
    # this migration ran, this is best-effort — they can re-PATCH it.
    op.execute(
        "UPDATE wallets "
        "SET purpose = 'portfolio', can_trade = FALSE "
        "WHERE wallet_type = 'perpdex' AND purpose = 'both'"
    )
