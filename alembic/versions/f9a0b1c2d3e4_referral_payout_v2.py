"""referral payout v2 — claim/unclaim earnings, status rename, 1-pending guard

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-05-02

Schema changes:
- referral_earnings.payout_request_id: nullable FK → referral_payout_requests.id
  Lets us link earnings to the payout that "claimed" them. Available
  balance = sum(earnings WHERE payout_request_id IS NULL).
- referral_payout_requests.status: rename existing rows from
  'paid' → 'completed', 'rejected' → 'cancelled'. New default stays
  'pending'. Application code only writes the new names from now on.
- Partial UNIQUE index on (user_id) WHERE status='pending' so two
  parallel POST /payout requests can't both insert.
"""
from alembic import op
import sqlalchemy as sa


revision = 'f9a0b1c2d3e4'
down_revision = 'e8f9a0b1c2d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. Earnings → payout link
    with op.batch_alter_table('referral_earnings') as batch:
        batch.add_column(sa.Column('payout_request_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_earnings_payout_request',
            'referral_payout_requests',
            ['payout_request_id'], ['id'],
            ondelete='SET NULL',
        )
    op.create_index(
        'ix_referral_earnings_payout_request',
        'referral_earnings',
        ['payout_request_id'],
    )

    # 2. Rename existing payout statuses to the new vocabulary so the
    # service code (which only writes new names) doesn't have to deal
    # with mixed-case data.
    op.execute(
        "UPDATE referral_payout_requests SET status='completed' WHERE status='paid'"
    )
    op.execute(
        "UPDATE referral_payout_requests SET status='cancelled' WHERE status='rejected'"
    )

    # 3. Partial UNIQUE on (user_id) WHERE status='pending'. Postgres
    # supports it; SQLite (used in tests) doesn't — fall back to a
    # plain composite index there. Application-level guard still applies.
    if dialect == 'postgresql':
        op.execute(
            "CREATE UNIQUE INDEX ux_one_pending_payout_per_user "
            "ON referral_payout_requests (user_id) WHERE status = 'pending'"
        )
    else:
        op.create_index(
            'ux_one_pending_payout_per_user',
            'referral_payout_requests',
            ['user_id', 'status'],
        )

    # 4. Belt-and-braces CHECK constraint: lock status to the new
    # vocabulary so a stray INSERT can't reintroduce the old strings.
    # Postgres only — SQLite doesn't support adding CHECKs to existing
    # tables cleanly; the service-layer guard covers it there.
    if dialect == 'postgresql':
        op.execute(
            "ALTER TABLE referral_payout_requests "
            "ADD CONSTRAINT chk_payout_status "
            "CHECK (status IN ('pending', 'completed', 'cancelled'))"
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == 'postgresql':
        op.execute(
            "ALTER TABLE referral_payout_requests DROP CONSTRAINT IF EXISTS chk_payout_status"
        )
    op.drop_index('ux_one_pending_payout_per_user', table_name='referral_payout_requests')
    op.execute(
        "UPDATE referral_payout_requests SET status='paid' WHERE status='completed'"
    )
    op.execute(
        "UPDATE referral_payout_requests SET status='rejected' WHERE status='cancelled'"
    )
    op.drop_index('ix_referral_earnings_payout_request', table_name='referral_earnings')
    with op.batch_alter_table('referral_earnings') as batch:
        batch.drop_constraint('fk_earnings_payout_request', type_='foreignkey')
        batch.drop_column('payout_request_id')
