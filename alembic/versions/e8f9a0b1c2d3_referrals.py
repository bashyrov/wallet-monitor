"""referral program — code + earnings + payouts

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-04-30
"""
from alembic import op
import sqlalchemy as sa


revision = 'e8f9a0b1c2d3'
down_revision = 'd7e8f9a0b1c2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('referral_code', sa.String(), nullable=True))
    op.add_column('users', sa.Column('referred_by_id', sa.Integer(), nullable=True))
    op.add_column('users', sa.Column('referral_pct_override', sa.Float(), nullable=True))
    op.add_column('users', sa.Column('referral_payout_address', sa.String(), nullable=True))
    op.create_index('ix_users_referral_code', 'users', ['referral_code'], unique=True)
    op.create_index('ix_users_referred_by_id', 'users', ['referred_by_id'])
    with op.batch_alter_table('users') as batch:
        batch.create_foreign_key(
            'fk_users_referred_by',
            'users',
            ['referred_by_id'], ['id'],
            ondelete='SET NULL',
        )

    op.create_table(
        'referral_earnings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('referrer_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('referee_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('payment_id', sa.Integer(),
                  sa.ForeignKey('payments.id', ondelete='SET NULL'),
                  nullable=True, unique=True),
        sa.Column('pct', sa.Float(), nullable=False),
        sa.Column('amount_usd', sa.Numeric(14, 2), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_referral_earnings_referrer', 'referral_earnings', ['referrer_id'])

    op.create_table(
        'referral_payout_requests',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('amount_usd', sa.Numeric(14, 2), nullable=False),
        sa.Column('address', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='pending'),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_referral_payouts_user', 'referral_payout_requests', ['user_id'])
    op.create_index('ix_referral_payouts_status', 'referral_payout_requests', ['status'])


def downgrade() -> None:
    op.drop_index('ix_referral_payouts_status', table_name='referral_payout_requests')
    op.drop_index('ix_referral_payouts_user', table_name='referral_payout_requests')
    op.drop_table('referral_payout_requests')
    op.drop_index('ix_referral_earnings_referrer', table_name='referral_earnings')
    op.drop_table('referral_earnings')
    with op.batch_alter_table('users') as batch:
        batch.drop_constraint('fk_users_referred_by', type_='foreignkey')
    op.drop_index('ix_users_referred_by_id', table_name='users')
    op.drop_index('ix_users_referral_code', table_name='users')
    op.drop_column('users', 'referral_payout_address')
    op.drop_column('users', 'referral_pct_override')
    op.drop_column('users', 'referred_by_id')
    op.drop_column('users', 'referral_code')
