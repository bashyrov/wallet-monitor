"""plans cleanup: match /pricing page (Free + Screener-only + Screener+Portfolio + Unlim)

Revision ID: s2t3u4v5w6x7
Revises: r1s2t3u4v5w6
Create Date: 2026-06-25

Drops the inactive legacy tiers (Pro / Platinum / Enterprise) that were
seeded by earlier migrations but never sold — pricing page only shows
free / screener / full / unlim.

Also:
- Renames screener → "Screener only" and full → "Screener + Portfolio"
  to match the /pricing copy verbatim. Descriptions tightened.
- Migrates legacy users.plan='basic' rows to 'free' + sets plan_id so
  the legacy plan-string code path can be retired safely. plan='basic'
  was the registration default before plan_id became authoritative; a
  handful of pre-cutover accounts still carry it.

Safe to apply: the 3 inactive plans have 0 users.plan_id references
(verified before the migration was written), payments.plan_id is
ondelete=RESTRICT but those plans have 0 payments too.
"""
from alembic import op
import sqlalchemy as sa


revision = 's2t3u4v5w6x7'
down_revision = 'r1s2t3u4v5w6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Migrate legacy users.plan='basic' rows to 'free'. Set both the
    # legacy string column and plan_id so future reads via either path
    # see the same value.
    bind.execute(sa.text("""
        UPDATE users
           SET plan = 'free',
               plan_id = (SELECT id FROM plans WHERE slug = 'free' LIMIT 1)
         WHERE plan = 'basic'
    """))

    # 2. Rename screener + full to the /pricing copy. Descriptions match
    # the marketing wording: 'Screener only' is the explicit name for
    # the no-portfolio tier, 'Screener + Portfolio' for the full tier.
    bind.execute(sa.text("""
        UPDATE plans
           SET name = 'Screener only',
               description = 'Full live screener + trading, no portfolio tracking'
         WHERE slug = 'screener'
    """))
    bind.execute(sa.text("""
        UPDATE plans
           SET name = 'Screener + Portfolio',
               description = 'Everything: Screener + Portfolio across CEX, on-chain and DEX'
         WHERE slug = 'full'
    """))

    # 3. Delete the inactive legacy plans. Done last so the previous
    # UPDATEs can't accidentally reference them. ondelete=RESTRICT on
    # payments.plan_id and users.plan_id means this errors loudly if
    # there's a row we missed — that's the right safety behaviour.
    bind.execute(sa.text("""
        DELETE FROM plans
         WHERE slug IN ('pro', 'platinum', 'enterprise')
    """))


def downgrade() -> None:
    bind = op.get_bind()
    # Re-create the deleted rows in their previously-active state. Values
    # mirror q3r4s5t6u7v8_pricing_promos_popups + r4s5t6u7v8w9 originals.
    bind.execute(sa.text("""
        INSERT INTO plans (slug, name, description, price_usd_monthly, price_usd_annual,
                            portfolio_limit, portfolio_limit_grace, exchange_keys_per_venue,
                            trade_delay_ms, has_portfolio, is_subscription, is_admin_only,
                            features, is_free, is_active, sort_order)
        VALUES
          ('pro',        'Pro',        NULL, 5,  48,  30, 5, 3, 0, true,  true, false, NULL, false, false, 10),
          ('platinum',   'Platinum',   NULL, 10, 96,  30, 5, 3, 0, true,  true, false, NULL, false, false, 11),
          ('enterprise', 'Enterprise', NULL, 25, 240, 30, 5, 3, 0, true,  true, false, NULL, false, false, 12)
        ON CONFLICT (slug) DO NOTHING
    """))
    bind.execute(sa.text("""
        UPDATE plans SET name='Screener',
                          description='Full live screener + trading, no portfolio tracking'
         WHERE slug='screener'
    """))
    bind.execute(sa.text("""
        UPDATE plans SET name='Full',
                          description='Everything: Screener + Portfolio across CEX, on-chain and DEX'
         WHERE slug='full'
    """))
    # Leave the basic→free user migration alone; reversing it would set
    # users on the legacy slug back which is meaningless after retirement.
