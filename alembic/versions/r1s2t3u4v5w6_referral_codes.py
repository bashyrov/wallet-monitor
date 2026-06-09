"""split-discount referral codes (ReferralCode + Registration + Usage)

Revision ID: r1s2t3u4v5w6
Revises: n3o4p5q6r7s8
Create Date: 2026-06-10

NEW SYSTEM — three tables + one User column. Replaces the fixed-20% accrual
mechanism for any user who registers AFTER this migration runs.

OLD SYSTEM IS PRESERVED:
- users.referral_code / referred_by_id / referral_pct_override — kept on row
  (existing users keep them) but no new auto-generation.
- referral_earnings / referral_payout_requests — untouched; old balances stay
  payable through the existing payout flow.
- _activate_user logic gates new vs old by users.signup_code_id presence
  (set → new flow, NULL → old referred_by_id frozen / no-op).

DB INVARIANTS (CHECK constraints — un-bypassable, raw INSERT must respect):
1. commission_pct >= 0 AND discount_pct >= 0
2. commission_pct + discount_pct <= 45        (global cap, regardless of code type)
3. created_by_admin_id IS NOT NULL OR
   (commission_pct + discount_pct <= 25)      (high pool requires proven admin)
4. code_type IN ('self_serve', 'admin')
5. (code_type='self_serve' AND created_by_admin_id IS NULL)
   OR
   (code_type='admin' AND created_by_admin_id IS NOT NULL)
   (type ↔ admin_id consistency — defends against forged JSON)

Case-insensitive uniqueness on `code`: functional UNIQUE index on LOWER(code).
Original casing kept in the column for display ("CryptoPro" stays mixed).

Per-referee anti-reattribution: UNIQUE(referee_id) on registrations table —
a user can be bound to AT MOST ONE code, ever. Re-register attempts blocked
at the schema layer.

Per-payment idempotency: UNIQUE(payment_id) on usages table — webhook retry
or double-call cannot create two ledger rows for one payment.
"""
from alembic import op
import sqlalchemy as sa


revision = 'r1s2t3u4v5w6'
down_revision = 'n3o4p5q6r7s8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── referral_codes ──────────────────────────────────────────────────────
    op.create_table(
        'referral_codes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('owner_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('code', sa.String(length=32), nullable=False),
        sa.Column('commission_pct', sa.Numeric(5, 2), nullable=False),
        sa.Column('discount_pct', sa.Numeric(5, 2), nullable=False),
        sa.Column('code_type', sa.String(length=16), nullable=False),
        sa.Column('created_by_admin_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('created_at', sa.DateTime(),
                  nullable=False, server_default=sa.func.now()),

        # invariant 1: non-negative components
        sa.CheckConstraint(
            'commission_pct >= 0 AND discount_pct >= 0',
            name='ck_referral_codes_nonneg',
        ),
        # invariant 2: global cap
        sa.CheckConstraint(
            'commission_pct + discount_pct <= 45',
            name='ck_referral_codes_total_cap',
        ),
        # invariant 3: high pool requires admin attribution
        sa.CheckConstraint(
            'created_by_admin_id IS NOT NULL '
            'OR (commission_pct + discount_pct <= 25)',
            name='ck_referral_codes_high_pool_needs_admin',
        ),
        # invariant 4: code_type enum
        sa.CheckConstraint(
            "code_type IN ('self_serve', 'admin')",
            name='ck_referral_codes_type_enum',
        ),
        # invariant 5: type ↔ admin_id consistency
        sa.CheckConstraint(
            "(code_type = 'self_serve' AND created_by_admin_id IS NULL) "
            "OR (code_type = 'admin' AND created_by_admin_id IS NOT NULL)",
            name='ck_referral_codes_type_admin_match',
        ),
    )
    # Case-insensitive uniqueness via functional index — works on both PG and
    # SQLite. Crypto / crypto cannot coexist; original casing preserved in
    # the column for display.
    op.create_index(
        'uq_referral_codes_lower',
        'referral_codes',
        [sa.text('LOWER(code)')],
        unique=True,
    )

    # ── referral_code_registrations ─────────────────────────────────────────
    # One row per (code, referee) where the referee registered using the code.
    # UNIQUE(referee_id) is the load-bearing invariant: a user can have AT
    # MOST ONE binding ever — reattribution is physically impossible at the
    # DB layer.
    op.create_table(
        'referral_code_registrations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code_id', sa.Integer(),
                  sa.ForeignKey('referral_codes.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('referee_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('created_at', sa.DateTime(),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('referee_id', name='uq_reg_referee'),
        sa.UniqueConstraint('code_id', 'referee_id', name='uq_reg_code_referee'),
    )

    # ── referral_code_usages ───────────────────────────────────────────────
    # Append-only ledger of paid invoices that used a code. UNIQUE(payment_id)
    # is the idempotency seal — webhook retry / double-process cannot double-
    # credit. reversed_at flips on refund (refund decrements the 5-per-referee
    # counter so the referee can buy again).
    op.create_table(
        'referral_code_usages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code_id', sa.Integer(),
                  sa.ForeignKey('referral_codes.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('referee_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('payment_id', sa.Integer(),
                  sa.ForeignKey('payments.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('payment_amount_usd', sa.Numeric(14, 2), nullable=False),
        sa.Column('commission_earned', sa.Numeric(14, 2), nullable=False),
        sa.Column('discount_applied', sa.Numeric(14, 2), nullable=False),
        sa.Column('created_at', sa.DateTime(),
                  nullable=False, server_default=sa.func.now()),
        sa.Column('reversed_at', sa.DateTime(), nullable=True),
        sa.Column('reversal_reason', sa.String(), nullable=True),
        sa.UniqueConstraint('payment_id', name='uq_usage_payment'),
    )
    # Hot lookup: count of non-reversed usages by (code, referee) for the
    # 5-per-referee cap check. Index keeps the count cheap.
    op.create_index(
        'ix_usage_code_referee',
        'referral_code_usages',
        ['code_id', 'referee_id'],
    )

    # ── users.signup_code_id ───────────────────────────────────────────────
    # The code the user picked at registration. NULL means "registered
    # without a code" OR "legacy user pre-r1s2t3u4v5w6". Compute paths gate
    # new-flow vs old-flow on (signup_code_id IS NOT NULL).
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('signup_code_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_users_signup_code',
            'referral_codes',
            ['signup_code_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.drop_constraint('fk_users_signup_code', type_='foreignkey')
        batch.drop_column('signup_code_id')
    op.drop_index('ix_usage_code_referee', table_name='referral_code_usages')
    op.drop_table('referral_code_usages')
    op.drop_table('referral_code_registrations')
    op.drop_index('uq_referral_codes_lower', table_name='referral_codes')
    op.drop_table('referral_codes')
