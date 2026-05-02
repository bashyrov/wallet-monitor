"""payment refund + referral commission reversal

Revision ID: g0a1b2c3d4e5
Revises: f9a0b1c2d3e4
Create Date: 2026-05-02

Schema:
- payments.refunded_at / refunded_reason — set when admin (or webhook)
  marks a payment refunded. Status string adds 'refunded' to the
  existing pending/paid/failed/expired set. No CHECK enforced because
  the prod table doesn't have one.
- referral_earnings.reversed_at / reversal_reason — set on the ORIGINAL
  earning row when its payment is refunded. The original is never
  deleted (audit trail) — instead a sibling negative-amount earning row
  is inserted so SUM(amount_usd) keeps reflecting the truth.
- referral_earnings.reversal_of_id — FK self, points the negative
  sibling at its parent so admin can group them in reports.
"""
from alembic import op
import sqlalchemy as sa


revision = 'g0a1b2c3d4e5'
down_revision = 'f9a0b1c2d3e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('payments') as batch:
        batch.add_column(sa.Column('refunded_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('refunded_reason', sa.String(), nullable=True))

    with op.batch_alter_table('referral_earnings') as batch:
        batch.add_column(sa.Column('reversed_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('reversal_reason', sa.String(), nullable=True))
        batch.add_column(sa.Column('reversal_of_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_earnings_reversal_of',
            'referral_earnings',
            ['reversal_of_id'], ['id'],
            ondelete='SET NULL',
        )
    op.create_index(
        'ix_referral_earnings_reversal_of',
        'referral_earnings',
        ['reversal_of_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_referral_earnings_reversal_of', table_name='referral_earnings')
    with op.batch_alter_table('referral_earnings') as batch:
        batch.drop_constraint('fk_earnings_reversal_of', type_='foreignkey')
        batch.drop_column('reversal_of_id')
        batch.drop_column('reversal_reason')
        batch.drop_column('reversed_at')

    with op.batch_alter_table('payments') as batch:
        batch.drop_column('refunded_reason')
        batch.drop_column('refunded_at')
