"""billing_periods table + plans realignment (Free / Screener / Full).

The pricing page model is "one product, two configurations, four
commitment lengths" — not a tiered ladder. Replace the abstract
free/pro/platinum/enterprise scaffolding with the actual product
shape.

  · plans gets a `has_portfolio` flag — Screener-only plans flip this
    off, the portfolio scan endpoint then returns 402 for them.
  · billing_periods is its own table so admins can tune the per-period
    discount in /admin without a deploy.
  · payments gets billing_period_id; the legacy billing_cycle string
    column stays nullable for backward-compat but new flows write the
    FK instead.

Revision ID: s5t6u7v8w9x0
Revises: r4s5t6u7v8w9
Create Date: 2026-04-25
"""
from alembic import op
import sqlalchemy as sa
import json as _json

revision = 's5t6u7v8w9x0'
down_revision = 'r4s5t6u7v8w9'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. plans.has_portfolio + plans.is_subscription ───────────────────
    op.add_column("plans",
        sa.Column("has_portfolio", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("plans",
        sa.Column("is_subscription", sa.Boolean(), nullable=False, server_default=sa.true()))

    # ── 2. billing_periods table ─────────────────────────────────────────
    op.create_table(
        "billing_periods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False, unique=True),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("months", sa.Integer(), nullable=False),
        sa.Column("discount_pct", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_billing_periods_slug", "billing_periods", ["slug"], unique=True)
    op.create_index("ix_billing_periods_is_active", "billing_periods", ["is_active"])

    # ── 3. payments.billing_period_id (nullable) ─────────────────────────
    op.add_column("payments",
        sa.Column("billing_period_id", sa.Integer(),
                  sa.ForeignKey("billing_periods.id", ondelete="RESTRICT"),
                  nullable=True))

    # ── Seed billing periods ─────────────────────────────────────────────
    bind = op.get_bind()
    bind.execute(sa.text("""
        INSERT INTO billing_periods (slug, label, months, discount_pct, sort_order, is_active, created_at, updated_at) VALUES
            ('monthly',     'Monthly',     1,   0, 0, TRUE, NOW(), NOW()),
            ('quarterly',   'Quarterly',   3,   5, 1, TRUE, NOW(), NOW()),
            ('semi_annual', 'Semi-annual', 6,  15, 2, TRUE, NOW(), NOW()),
            ('yearly',      'Yearly',     12,  25, 3, TRUE, NOW(), NOW())
    """))

    # ── 4. Realign plans: Free / Screener / Full ─────────────────────────
    full_features = {
        "perks": [
            "Live Long/Short, Spot/Short and DEX/Short scanners",
            "Up to 30 portfolio wallets across CEX / on-chain / perp DEX",
            "1-click trading on 14+ venues with zero-latency execution",
            "Up to 3 API keys per exchange + main-key selector",
            "Spread alerts via Telegram",
        ],
        "limits": [],
    }
    screener_features = {
        "perks": [
            "Live Long/Short, Spot/Short and DEX/Short scanners",
            "1-click trading on 14+ venues with zero-latency execution",
            "Up to 3 API keys per exchange + main-key selector",
            "Spread alerts via Telegram",
        ],
        "limits": [
            "No portfolio tracking — switch to Full to add portfolio wallets",
        ],
    }
    free_features = {
        "perks": [
            "5 portfolio wallets",
            "1 API key per exchange",
            "Live screener (2-min preview before sign-in)",
            "Spread alerts via Telegram",
        ],
        "limits": [
            "Trading orders run with +500 ms delay",
            "Promo codes don't apply (already free)",
        ],
    }

    # Mark obsolete tiers inactive (stays in DB for any historic Payment FKs).
    bind.execute(sa.text("""
        UPDATE plans SET is_active = FALSE
         WHERE slug IN ('pro', 'platinum', 'enterprise', 'max')
    """))

    # Upsert: free / screener / full
    plans = [
        ("free",      "Free",      "Try Avalant — no card required",
         0,    True,  5,  5, 1, 500, free_features,     0,  False),
        ("screener",  "Screener",  "Full live screener + trading, no portfolio tracking",
         55,   False, 0,  5, 3, 0,   screener_features, 10, False),
        ("full",      "Full",      "Everything: Screener + Portfolio across CEX, on-chain and DEX",
         65,   False, 30, 5, 3, 0,   full_features,     20, True),
    ]
    for slug, name, desc, base_mo, has_portfolio, pf, grace, keys, delay, feats, sort, is_full in plans:
        existing = bind.execute(sa.text("SELECT id FROM plans WHERE slug=:s"), {"s": slug}).fetchone()
        if existing:
            bind.execute(sa.text("""
                UPDATE plans SET
                  name = :name,
                  description = :desc,
                  price_usd_monthly = :mo,
                  price_usd_annual = 0,
                  portfolio_limit = :pf,
                  portfolio_limit_grace = :grace,
                  exchange_keys_per_venue = :keys,
                  trade_delay_ms = :delay,
                  features = CAST(:feats AS JSON),
                  is_free = :is_free,
                  has_portfolio = :has_portfolio,
                  is_active = TRUE,
                  is_subscription = TRUE,
                  sort_order = :sort,
                  updated_at = NOW()
                WHERE slug = :slug
            """), {"name": name, "desc": desc, "mo": base_mo,
                   "pf": pf, "grace": grace, "keys": keys, "delay": delay,
                   "feats": _json.dumps(feats),
                   "is_free": (slug == "free"),
                   "has_portfolio": has_portfolio,
                   "sort": sort, "slug": slug})
        else:
            bind.execute(sa.text("""
                INSERT INTO plans
                  (slug, name, description, price_usd_monthly, price_usd_annual,
                   portfolio_limit, portfolio_limit_grace, exchange_keys_per_venue,
                   trade_delay_ms, features, is_free, has_portfolio, is_active,
                   is_subscription, sort_order, created_at, updated_at)
                VALUES
                  (:slug, :name, :desc, :mo, 0, :pf, :grace, :keys, :delay,
                   CAST(:feats AS JSON), :is_free, :has_portfolio, TRUE, TRUE,
                   :sort, NOW(), NOW())
            """), {"slug": slug, "name": name, "desc": desc, "mo": base_mo,
                   "pf": pf, "grace": grace, "keys": keys, "delay": delay,
                   "feats": _json.dumps(feats),
                   "is_free": (slug == "free"),
                   "has_portfolio": has_portfolio,
                   "sort": sort})

    # Re-stamp users that pointed at a now-inactive plan to 'free' so the
    # portfolio scan keeps working until they actively re-subscribe.
    bind.execute(sa.text("""
        UPDATE users SET plan_id = (SELECT id FROM plans WHERE slug='free')
        WHERE plan_id IN (SELECT id FROM plans WHERE is_active = FALSE)
    """))


def downgrade():
    # data-only refresh — leave the structure as-is on downgrade.
    op.drop_column("payments", "billing_period_id")
    op.drop_index("ix_billing_periods_is_active", table_name="billing_periods")
    op.drop_index("ix_billing_periods_slug", table_name="billing_periods")
    op.drop_table("billing_periods")
    op.drop_column("plans", "is_subscription")
    op.drop_column("plans", "has_portfolio")
