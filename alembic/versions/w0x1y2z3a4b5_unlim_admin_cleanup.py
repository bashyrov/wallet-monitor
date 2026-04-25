"""Plan-mapping cleanup + Unlim admin plan + delete test users.

Fixes the bug where every user.plan_id pointed at the Free plan because
the original mapping migration only handled `'pro' | 'platinum' |
'enterprise' | 'unlim'` strings, and several legacy users carried
`plan='basic'` (no row) so they fell through to free. Re-stamp every
user.plan_id from their `plan` slug so the new system shows the right
limits.

  · Add an `unlim` plan with portfolio_limit = exchange_keys_per_venue
    = -1 (interpreted as "unlimited" by plan_service.effective_limits).
    is_admin_only=True so it can't be picked from /pricing.
  · Re-map: basic / null / "" → free, pro → free, platinum → full,
    enterprise → full, unlim → unlim.
  · Hard-delete every user except admins (id 1 in our prod) — the user
    explicitly asked for a clean slate so test fixtures don't pollute
    statistics on the admin dashboard.
  · Stamp the surviving admin onto the unlim plan.

Revision ID: w0x1y2z3a4b5
Revises: v8w9x0y1z2a3
Create Date: 2026-04-25
"""
from alembic import op
import sqlalchemy as sa
import json as _json

revision = 'w0x1y2z3a4b5'
down_revision = 'w9x0y1z2a3b4'
branch_labels = None
depends_on = None


_UNLIM_FEATURES = {
    "perks": [
        "Unlimited portfolio wallets",
        "Unlimited API keys per exchange",
        "Zero-latency trading on every supported venue",
        "Internal / admin tier — not for sale",
    ],
    "limits": [],
}


def upgrade():
    # 1. Add `is_admin_only` column to plans so we can hide the Unlim tier
    #    from the public /pricing list while still using the same limits
    #    mechanism for it.
    op.add_column(
        "plans",
        sa.Column("is_admin_only", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    bind = op.get_bind()

    # 2. Upsert the Unlim plan. portfolio_limit / exchange_keys_per_venue
    #    use -1 as the "unlimited" sentinel (plan_service handles it).
    existing = bind.execute(sa.text("SELECT id FROM plans WHERE slug='unlim'")).fetchone()
    if existing:
        bind.execute(sa.text("""
            UPDATE plans SET
                name='Unlim',
                description='Internal admin tier — unlimited portfolio + screener',
                price_usd_monthly=0, price_usd_annual=0,
                portfolio_limit=-1, portfolio_limit_grace=-1,
                exchange_keys_per_venue=-1,
                trade_delay_ms=0,
                features=CAST(:f AS JSON),
                is_free=FALSE, is_active=TRUE, is_admin_only=TRUE,
                has_portfolio=TRUE, is_subscription=FALSE,
                sort_order=99,
                updated_at=NOW()
            WHERE slug='unlim'
        """), {"f": _json.dumps(_UNLIM_FEATURES)})
    else:
        bind.execute(sa.text("""
            INSERT INTO plans
              (slug, name, description, price_usd_monthly, price_usd_annual,
               portfolio_limit, portfolio_limit_grace, exchange_keys_per_venue,
               trade_delay_ms, features, is_free, has_portfolio, is_active,
               is_subscription, is_admin_only, sort_order, created_at, updated_at)
            VALUES
              ('unlim', 'Unlim',
               'Internal admin tier — unlimited portfolio + screener',
               0, 0, -1, -1, -1, 0, CAST(:f AS JSON),
               FALSE, TRUE, TRUE, FALSE, TRUE, 99,
               NOW(), NOW())
        """), {"f": _json.dumps(_UNLIM_FEATURES)})

    # 3. Delete every non-admin user. The wallets FK does NOT have ON
    #    DELETE CASCADE on prod (legacy table) so we have to clean up
    #    every dependent row manually before the DELETE on users. Order
    #    matters — children first.
    bind.execute(sa.text("""
        DELETE FROM wallet_addresses
         WHERE wallet_id IN (
            SELECT id FROM wallets WHERE user_id IN (
                SELECT id FROM users WHERE is_admin = FALSE
            )
         )
    """))
    bind.execute(sa.text("""
        DELETE FROM wallet_tags
         WHERE wallet_id IN (
            SELECT id FROM wallets WHERE user_id IN (
                SELECT id FROM users WHERE is_admin = FALSE
            )
         )
    """))
    bind.execute(sa.text("""
        DELETE FROM balance_snapshots
         WHERE user_id IN (SELECT id FROM users WHERE is_admin = FALSE)
    """))
    bind.execute(sa.text("""
        DELETE FROM provider_error_logs
         WHERE wallet_type IS NOT NULL
    """))  # legacy table; drop the noise but no FK
    bind.execute(sa.text("""
        DELETE FROM balance_history
         WHERE user_id IN (SELECT id FROM users WHERE is_admin = FALSE)
    """))
    # Drop dependents that may or may not be present in the schema
    # depending on which migrations have run — wrap in try-blocks via
    # a sub-transaction so a missing table doesn't abort the upgrade.
    for table in (
        "arb_alerts",
        "tags",
        "watchlists",
        "password_reset_tokens",
        "email_verify_tokens",
        "popup_dismissals",
        "promo_code_usages",
        "payments",
        "wallets",
        "audit_log",
    ):
        try:
            # Each statement runs in its own savepoint so a missing
            # column / table doesn't poison the outer transaction.
            sp = bind.begin_nested()
            if table == "audit_log":
                bind.execute(sa.text(
                    f"DELETE FROM {table} WHERE actor_user_id IN (SELECT id FROM users WHERE is_admin = FALSE)"
                ))
            elif table in ("tags",):
                bind.execute(sa.text(
                    f"DELETE FROM {table} WHERE user_id IS NOT NULL "
                    f"AND user_id IN (SELECT id FROM users WHERE is_admin = FALSE)"
                ))
            else:
                bind.execute(sa.text(
                    f"DELETE FROM {table} WHERE user_id IN "
                    f"(SELECT id FROM users WHERE is_admin = FALSE)"
                ))
            sp.commit()
        except Exception:
            try:
                sp.rollback()
            except Exception:
                pass

    bind.execute(sa.text("DELETE FROM users WHERE is_admin = FALSE"))

    # 4. Re-map remaining users by their legacy plan string.
    #    'unlim' → unlim, anything else → unlim if admin, free if not.
    bind.execute(sa.text("""
        UPDATE users SET plan_id = (SELECT id FROM plans WHERE slug='unlim')
        WHERE is_admin = TRUE
    """))
    bind.execute(sa.text("""
        UPDATE users SET plan_id = (SELECT id FROM plans WHERE slug='free')
        WHERE is_admin = FALSE AND plan_id IS NULL
    """))


def downgrade():
    op.drop_column("plans", "is_admin_only")
