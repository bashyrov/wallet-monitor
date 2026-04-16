"""add wallets.purpose (portfolio | screener); backfill existing can_trade=true rows as 'screener'

Revision ID: m9n0o1p2q3r4
Revises: l8m9n0o1p2q3
Create Date: 2026-04-17 00:30:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'm9n0o1p2q3r4'
down_revision = 'l8m9n0o1p2q3'
branch_labels = None
depends_on = None


def upgrade():
    # SQLite-friendly: add nullable then backfill then don't alter NOT NULL (SQLite can't easily).
    op.add_column('wallets', sa.Column('purpose', sa.String(), nullable=False, server_default='portfolio'))
    # Backfill: exchange wallets with can_trade=true → 'screener'
    op.execute("UPDATE wallets SET purpose='screener' WHERE wallet_type='exchange' AND can_trade=true")


def downgrade():
    op.drop_column('wallets', 'purpose')
