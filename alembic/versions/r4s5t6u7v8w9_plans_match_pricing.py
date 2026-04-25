"""rename max → platinum, set canonical pricing (Pro $5/$48, Platinum $10/$96),
add Enterprise tier, attach descriptions for the pricing page.

Revision ID: r4s5t6u7v8w9
Revises: q3r4s5t6u7v8
Create Date: 2026-04-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'r4s5t6u7v8w9'
down_revision = 'q3r4s5t6u7v8'
branch_labels = None
depends_on = None


_FREE_FEATURES = {
    "perks": [
        "5 portfolio wallets",
        "1 API key per exchange",
        "Live screener (2-min preview before sign-in)",
        "Spread alerts via Telegram",
    ],
    "limits": [
        "Trading orders run with +500ms delay",
        "Promo codes don't apply (already free)",
    ],
}

_PRO_FEATURES = {
    "perks": [
        "30 portfolio wallets",
        "Up to 3 API keys per exchange + main-key selector",
        "Zero-latency trading (no 500ms delay)",
        "Same live screener as everyone",
        "Priority Telegram alerts",
    ],
    "limits": [],
}

_PLATINUM_FEATURES = {
    "perks": [
        "Everything in Pro",
        "Custom popups disabled",
        "Early access to new exchanges",
        "Priority customer support",
    ],
    "limits": [],
}

_ENTERPRISE_FEATURES = {
    "perks": [
        "Everything in Platinum",
        "API access for programmatic balance / trade calls",
        "Dedicated Slack / Telegram channel",
        "Custom integrations",
        "SLA-backed uptime",
    ],
    "limits": [],
}


def upgrade():
    bind = op.get_bind()

    # 1. Drop any leftover plan slugs we no longer surface (defensive — only
    #    `max` from the previous seed exists in practice, but make this idempotent).
    bind.execute(sa.text("UPDATE users SET plan_id = NULL WHERE plan_id IN (SELECT id FROM plans WHERE slug='max')"))
    bind.execute(sa.text("UPDATE plans SET slug='platinum', name='Platinum' WHERE slug='max'"))

    # 2. Make sure the canonical 4 plans exist with the right values.
    plans = [
        ("free",       "Free",       "Get a feel for Avalant — no card required",
         0, 0, 5, 5, 1, 500, _FREE_FEATURES, True, 0),
        ("pro",        "Pro",        "For active traders who want zero-latency execution",
         5, 48, 30, 5, 3, 0, _PRO_FEATURES, False, 10),
        ("platinum",   "Platinum",   "Everything in Pro plus priority access",
         10, 96, 30, 5, 3, 0, _PLATINUM_FEATURES, False, 20),
        ("enterprise", "Enterprise", "API access, dedicated support, SLA — for desks",
         25, 240, 30, 5, 3, 0, _ENTERPRISE_FEATURES, False, 30),
    ]
    for slug, name, desc, mo, yr, pf, grace, keys, delay, feats, is_free, sort in plans:
        existing = bind.execute(sa.text("SELECT id FROM plans WHERE slug=:s"), {"s": slug}).fetchone()
        if existing:
            bind.execute(sa.text("""
                UPDATE plans SET
                  name = :name,
                  description = :desc,
                  price_usd_monthly = :mo,
                  price_usd_annual = :yr,
                  portfolio_limit = :pf,
                  portfolio_limit_grace = :grace,
                  exchange_keys_per_venue = :keys,
                  trade_delay_ms = :delay,
                  features = CAST(:feats AS JSON),
                  is_free = :is_free,
                  is_active = TRUE,
                  sort_order = :sort,
                  updated_at = NOW()
                WHERE slug = :slug
            """), {"name": name, "desc": desc, "mo": mo, "yr": yr,
                   "pf": pf, "grace": grace, "keys": keys, "delay": delay,
                   "feats": __import__("json").dumps(feats),
                   "is_free": is_free, "sort": sort, "slug": slug})
        else:
            bind.execute(sa.text("""
                INSERT INTO plans
                  (slug, name, description, price_usd_monthly, price_usd_annual,
                   portfolio_limit, portfolio_limit_grace, exchange_keys_per_venue,
                   trade_delay_ms, features, is_free, is_active, sort_order,
                   created_at, updated_at)
                VALUES
                  (:slug, :name, :desc, :mo, :yr, :pf, :grace, :keys, :delay,
                   CAST(:feats AS JSON), :is_free, TRUE, :sort, NOW(), NOW())
            """), {"slug": slug, "name": name, "desc": desc, "mo": mo, "yr": yr,
                   "pf": pf, "grace": grace, "keys": keys, "delay": delay,
                   "feats": __import__("json").dumps(feats),
                   "is_free": is_free, "sort": sort})

    # 3. Re-stamp users that pointed at the dead `max` plan.
    bind.execute(sa.text(
        "UPDATE users SET plan_id = (SELECT id FROM plans WHERE slug='platinum') "
        "WHERE plan_id IS NULL AND plan IN ('platinum', 'enterprise', 'unlim')"
    ))
    bind.execute(sa.text(
        "UPDATE users SET plan_id = (SELECT id FROM plans WHERE slug='free') "
        "WHERE plan_id IS NULL"
    ))


def downgrade():
    # No DB structure changes — this migration is data-only. Leave the rows
    # in place on downgrade rather than guessing at the previous values.
    pass
