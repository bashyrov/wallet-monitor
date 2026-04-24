"""pricing system: plans / payments / promo_codes / popups + wallets.is_main

Greenfield monetisation infra:
  · plans                 — admin-editable feature/limit catalogue
  · payments              — cryptocloud invoices and their lifecycle
  · promo_codes           — discount catalogue, per-code usage cap
  · promo_code_usages     — append-only ledger for stats
  · popups                — admin-managed promotion popups
  · popup_dismissals      — per-user dismiss state with frequency control

Plus: wallets.is_main (which exchange key is THE one for trading), and
users.plan_id FK to plans (string-based plan stays for backward compat
during the migration window).

Revision ID: q3r4s5t6u7v8
Revises: pr1a2b3c4d5e
Create Date: 2026-04-25
"""
from alembic import op
import sqlalchemy as sa

revision = 'q3r4s5t6u7v8'
down_revision = 'pr1a2b3c4d5e'
branch_labels = None
depends_on = None


def upgrade():
    # ── plans ────────────────────────────────────────────────────────────
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("price_usd_monthly", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("price_usd_annual", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("portfolio_limit", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("portfolio_limit_grace", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("exchange_keys_per_venue", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("trade_delay_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("features", sa.JSON(), nullable=True),
        sa.Column("is_free", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_plans_slug", "plans", ["slug"], unique=True)
    op.create_index("ix_plans_is_active", "plans", ["is_active"])

    # ── promo_codes ──────────────────────────────────────────────────────
    op.create_table(
        "promo_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(), nullable=False, unique=True),
        sa.Column("discount_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("applies_to_plan_ids", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_promo_codes_code", "promo_codes", ["code"], unique=True)
    op.create_index("ix_promo_codes_is_active", "promo_codes", ["is_active"])

    # ── payments ─────────────────────────────────────────────────────────
    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", sa.Integer(),
                  sa.ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("billing_cycle", sa.String(), nullable=False),
        sa.Column("base_amount_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column("discount_pct", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("final_amount_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column("promo_code_id", sa.Integer(),
                  sa.ForeignKey("promo_codes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("provider", sa.String(), nullable=False, server_default="cryptocloud"),
        sa.Column("provider_invoice_id", sa.String(), nullable=True, unique=True),
        sa.Column("provider_invoice_url", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("activated_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_payments_user_id", "payments", ["user_id"])
    op.create_index("ix_payments_status", "payments", ["status"])
    op.create_index("ix_payments_provider_invoice_id", "payments", ["provider_invoice_id"])

    # ── promo_code_usages (append-only ledger) ───────────────────────────
    op.create_table(
        "promo_code_usages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("promo_code_id", sa.Integer(),
                  sa.ForeignKey("promo_codes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payment_id", sa.Integer(),
                  sa.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", sa.Integer(),
                  sa.ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("discount_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_promo_usages_promo_id", "promo_code_usages", ["promo_code_id"])
    op.create_index("ix_promo_usages_user_id", "promo_code_usages", ["user_id"])

    # ── popups ───────────────────────────────────────────────────────────
    op.create_table(
        "popups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("button_text", sa.String(), nullable=False, server_default="View pricing"),
        sa.Column("button_url", sa.String(), nullable=False, server_default="/pricing"),
        sa.Column("target_type", sa.String(), nullable=False, server_default="all"),
        sa.Column("target_user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("frequency_type", sa.String(), nullable=False, server_default="once"),
        sa.Column("frequency_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_popups_is_active", "popups", ["is_active"])
    op.create_index("ix_popups_target_user_id", "popups", ["target_user_id"])

    # ── popup_dismissals ─────────────────────────────────────────────────
    op.create_table(
        "popup_dismissals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("popup_id", sa.Integer(),
                  sa.ForeignKey("popups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("popup_id", "user_id", name="uq_popup_dismissals_user_popup"),
    )
    op.create_index("ix_popup_dismissals_user_id", "popup_dismissals", ["user_id"])

    # ── wallets.is_main ──────────────────────────────────────────────────
    op.add_column(
        "wallets",
        sa.Column("is_main", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # ── users.plan_id FK ─────────────────────────────────────────────────
    op.add_column(
        "users",
        sa.Column("plan_id", sa.Integer(),
                  sa.ForeignKey("plans.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_users_plan_id", "users", ["plan_id"])

    # ── seed default plans (Free + Premium + Pro) ────────────────────────
    from datetime import datetime
    now = datetime.utcnow()
    plans_t = sa.table(
        "plans",
        sa.column("slug", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        sa.column("price_usd_monthly", sa.Numeric),
        sa.column("price_usd_annual", sa.Numeric),
        sa.column("portfolio_limit", sa.Integer),
        sa.column("portfolio_limit_grace", sa.Integer),
        sa.column("exchange_keys_per_venue", sa.Integer),
        sa.column("trade_delay_ms", sa.Integer),
        sa.column("features", sa.JSON),
        sa.column("is_free", sa.Boolean),
        sa.column("is_active", sa.Boolean),
        sa.column("sort_order", sa.Integer),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    op.bulk_insert(plans_t, [
        {
            "slug": "free", "name": "Free", "description": "Get started",
            "price_usd_monthly": 0, "price_usd_annual": 0,
            "portfolio_limit": 5, "portfolio_limit_grace": 5,
            "exchange_keys_per_venue": 1, "trade_delay_ms": 500,
            "features": {
                "perks": [
                    "5 portfolio wallets",
                    "1 API key per exchange",
                    "Live screener access (with 2-min anonymous cap)",
                    "Spread alerts via Telegram",
                ],
                "limits": [
                    "Trading orders run with +500ms delay",
                    "No promo codes accepted (already free)",
                ],
            },
            "is_free": True, "is_active": True, "sort_order": 0,
            "created_at": now, "updated_at": now,
        },
        {
            "slug": "pro", "name": "Pro", "description": "For active traders",
            "price_usd_monthly": 9, "price_usd_annual": 86,
            "portfolio_limit": 30, "portfolio_limit_grace": 5,
            "exchange_keys_per_venue": 3, "trade_delay_ms": 0,
            "features": {
                "perks": [
                    "30 portfolio wallets",
                    "Up to 3 API keys per exchange (with main-key selector)",
                    "Zero-latency trading",
                    "Custom popups disabled",
                    "Priority Telegram alerts",
                ],
                "limits": [
                    "Same screener data as everyone else (live for everyone)",
                ],
            },
            "is_free": False, "is_active": True, "sort_order": 10,
            "created_at": now, "updated_at": now,
        },
        {
            "slug": "max", "name": "Max", "description": "For desks and pro arbitrageurs",
            "price_usd_monthly": 29, "price_usd_annual": 278,
            "portfolio_limit": 30, "portfolio_limit_grace": 5,
            "exchange_keys_per_venue": 3, "trade_delay_ms": 0,
            "features": {
                "perks": [
                    "Everything in Pro",
                    "Priority customer support",
                    "Early access to new exchanges and features",
                    "API access for programmatic balance/trade calls",
                ],
                "limits": [],
            },
            "is_free": False, "is_active": True, "sort_order": 20,
            "created_at": now, "updated_at": now,
        },
    ])

    # ── backfill users.plan_id from users.plan string ────────────────────
    # Map old slugs (basic/pro/platinum/enterprise/unlim) to new plan ids
    op.execute("""
        UPDATE users SET plan_id = (SELECT id FROM plans WHERE slug='free')
        WHERE plan IN ('basic', 'free') OR plan IS NULL OR plan_id IS NULL
    """)
    op.execute("""
        UPDATE users SET plan_id = (SELECT id FROM plans WHERE slug='pro')
        WHERE plan = 'pro'
    """)
    op.execute("""
        UPDATE users SET plan_id = (SELECT id FROM plans WHERE slug='max')
        WHERE plan IN ('platinum', 'enterprise', 'unlim')
    """)


def downgrade():
    op.drop_index("ix_users_plan_id", table_name="users")
    op.drop_column("users", "plan_id")
    op.drop_column("wallets", "is_main")
    op.drop_index("ix_popup_dismissals_user_id", table_name="popup_dismissals")
    op.drop_table("popup_dismissals")
    op.drop_index("ix_popups_target_user_id", table_name="popups")
    op.drop_index("ix_popups_is_active", table_name="popups")
    op.drop_table("popups")
    op.drop_index("ix_promo_usages_user_id", table_name="promo_code_usages")
    op.drop_index("ix_promo_usages_promo_id", table_name="promo_code_usages")
    op.drop_table("promo_code_usages")
    op.drop_index("ix_payments_provider_invoice_id", table_name="payments")
    op.drop_index("ix_payments_status", table_name="payments")
    op.drop_index("ix_payments_user_id", table_name="payments")
    op.drop_table("payments")
    op.drop_index("ix_promo_codes_is_active", table_name="promo_codes")
    op.drop_index("ix_promo_codes_code", table_name="promo_codes")
    op.drop_table("promo_codes")
    op.drop_index("ix_plans_is_active", table_name="plans")
    op.drop_index("ix_plans_slug", table_name="plans")
    op.drop_table("plans")
