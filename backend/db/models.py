from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Column, Integer, SmallInteger, String, DateTime, Table, ForeignKey, JSON, Boolean, Float, Numeric, UniqueConstraint, CheckConstraint, Index, text
from sqlalchemy.orm import relationship

from backend.db.base import Base


wallet_tags = Table(
    "wallet_tags",
    Base.metadata,
    Column("wallet_id", Integer, ForeignKey("wallets.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, nullable=False, unique=True)
    email = Column(String, nullable=False, unique=True)
    hashed_password = Column(String, nullable=False)
    is_admin = Column(Boolean, nullable=False, default=False)
    is_blocked = Column(Boolean, nullable=False, default=False)
    plan = Column(String, nullable=False, default="basic")  # legacy slug — kept for old deserializers, source of truth is plan_id
    plan_id = Column(Integer, ForeignKey("plans.id", ondelete="SET NULL"), nullable=True, index=True)
    plan_expires_at = Column(DateTime, nullable=True)
    request_count = Column(Integer, nullable=False, default=0)
    last_active_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    email_verified_at = Column(DateTime, nullable=True)

    tg_username = Column(String, nullable=True)
    tg_chat_id = Column(Integer, nullable=True)   # filled after user runs /start to the bot
    tg_id = Column(Integer, nullable=True, index=True, unique=True)  # Telegram numeric user id (from widget / bot update)

    # TOTP 2FA (any user, not just admins). Secret is Fernet-encrypted at rest.
    # `totp_verified_at` acts as the "armed" flag — the secret is meaningful
    # only after the user proves they configured their authenticator by entering
    # one valid code. Login flow gates sessions on a second-factor check whenever
    # totp_verified_at is set.
    totp_secret_enc = Column(String, nullable=True)
    totp_verified_at = Column(DateTime, nullable=True)
    # 8 single-use bcrypt-hashed recovery codes. Generated once at verify-time,
    # shown to user once, then stored as hashes. User can spend one to log in
    # if they lose their authenticator. Regenerate-able via /me/2fa/recovery-codes.
    totp_recovery_codes = Column(JSON, nullable=True)
    # When the last successful TOTP code was used — surfaced on /profile for
    # security visibility (catches stolen-device scenarios).
    totp_last_used_at = Column(DateTime, nullable=True)
    # FALSE for users who registered exclusively via OAuth (Google) and have
    # never set a local password. Sensitive endpoints (/me/2fa/setup, /disable,
    # /recovery-codes/regenerate, account deletion) gate on either the local
    # password OR a one-time email-confirm code based on this flag — without
    # it, a Google-only user can't satisfy the password prompt because they
    # don't know the random one we minted at registration.
    has_password = Column(Boolean, nullable=False, default=True, server_default=sa.true())

    # Subscription auto-renewal. False = user clicked Cancel from /profile —
    # the plan keeps running until plan_expires_at, but expiry notifications
    # stop firing and we don't auto-bill on the next cycle. Defaults True so
    # legacy users keep getting reminders without an explicit opt-in.
    auto_renew = Column(Boolean, nullable=False, default=True)
    # Throttle key for the expiry notifier so a daemon-restart can't fire
    # a duplicate "expires in 2 days" message minutes after the previous one.
    expiry_notice_last_sent_at = Column(DateTime, nullable=True)
    # Per-account failed-password counter. Bumps on every wrong-password
    # login attempt against this user (regardless of source IP), resets to
    # 0 on a successful login. Hits the threshold → is_blocked=True and
    # the user has to contact support to unlock.
    failed_login_attempts = Column(Integer, nullable=False, default=0)

    # Referral program (Avashare).
    # `referral_code` — short uppercase string the user shares; auto-generated
    #   on first registration. UNIQUE; case-insensitive equality enforced via
    #   index on UPPER() in the migration.
    # `referred_by_id` — FK to the user who referred them. NULL for users who
    #   registered without a code (or whose code was invalid). Set once at
    #   registration time and never mutated — flipping it later would require
    #   reconciling all earnings ledgers and isn't worth the surface area.
    # `referral_pct_override` — admin-tunable per-user commission rate (0..100).
    #   NULL = use the global default (20%). Always read via
    #   referral_service.get_commission_pct(user) so the default lives in one
    #   place.
    # `referral_payout_address` — TRC20 USDT address the user has nominated
    #   for payouts. Validated by referral_service.verify_trc20_address before
    #   write — see service for the regex.
    referral_code = Column(String, nullable=True, unique=True, index=True)
    referred_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                            nullable=True, index=True)
    referral_pct_override = Column(Float, nullable=True)
    referral_payout_address = Column(String, nullable=True)
    # New split-discount system. NULL = registered without a code (or legacy
    # pre-r1s2t3u4v5w6 user). Set → all subsequent payments use this code's
    # discount + commission rates and count against its 5-per-referee cap.
    # See migrations/r1s2t3u4v5w6_referral_codes.py for invariants.
    signup_code_id = Column(Integer, ForeignKey("referral_codes.id", ondelete="SET NULL"),
                            nullable=True)

    wallets = relationship("Wallet", back_populates="user", cascade="all, delete-orphan")
    arb_alerts = relationship("ArbAlert", back_populates="user", cascade="all, delete-orphan")
    referrer = relationship("User", remote_side="User.id", foreign_keys=[referred_by_id])
    referral_earnings = relationship("ReferralEarning", foreign_keys="ReferralEarning.referrer_id",
                                     back_populates="referrer", cascade="all, delete-orphan")
    referral_payouts = relationship("ReferralPayoutRequest", foreign_keys="ReferralPayoutRequest.user_id",
                                    back_populates="user", cascade="all, delete-orphan")


class ReferralEarning(Base):
    """One row per credited commission. Source of truth for "earned" totals.

    Created by payment_service._activate_user only on successful, signature-
    verified webhook activations. Never created on cart math, promo math, or
    intent-to-pay events.

    `amount_usd` is a snapshot at credit time; the linked payment's amount
    can drift if we ever re-issue an invoice, so always sum from this column,
    not from payments.
    """
    __tablename__ = "referral_earnings"

    id = Column(Integer, primary_key=True, index=True)
    referrer_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    referee_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    payment_id = Column(Integer, ForeignKey("payments.id", ondelete="SET NULL"),
                        nullable=True, unique=True)
    pct = Column(Float, nullable=False)            # commission % at credit time
    amount_usd = Column(Numeric(14, 2), nullable=False)
    # When the user submits a payout request, every UNCLAIMED earning row
    # is linked to that payout (`payout_request_id` set). On admin cancel,
    # the link is cleared and the earnings return to the "available" pool.
    # On admin completion, the link stays — those earnings are paid out.
    # `available_balance = sum(amount_usd) WHERE payout_request_id IS NULL`.
    payout_request_id = Column(Integer,
                               ForeignKey("referral_payout_requests.id", ondelete="SET NULL"),
                               nullable=True, index=True)
    # Reversal bookkeeping. The original earning is never edited or
    # deleted — instead a sibling row with negative amount_usd is
    # inserted, and the original is stamped with reversed_at to mark it
    # as "credit revoked". `reversal_of_id` on the negative sibling
    # points back at the original so the admin UI can group them.
    # `payment_id` on the negative sibling is NULL (UNIQUE on
    # payment_id allows multiple NULLs).
    reversed_at = Column(DateTime, nullable=True)
    reversal_reason = Column(String, nullable=True)
    reversal_of_id = Column(Integer,
                            ForeignKey("referral_earnings.id", ondelete="SET NULL"),
                            nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    referrer = relationship("User", foreign_keys=[referrer_id], back_populates="referral_earnings")
    referee = relationship("User", foreign_keys=[referee_id])
    payout_request = relationship("ReferralPayoutRequest", foreign_keys=[payout_request_id])
    reversal_of = relationship("ReferralEarning", remote_side="ReferralEarning.id",
                               foreign_keys=[reversal_of_id])


class ReferralPayoutRequest(Base):
    """User-initiated payout request. Admin reviews + marks as paid manually.

    Lifecycle: pending → paid (admin clicks "mark as done") OR pending →
    rejected (admin can reject with a reason; user can re-submit later).

    Available balance at any point in time = sum(earnings) − sum(paid +
    pending payouts). The pending guard prevents double-spend across two
    submissions before admin confirms the first.
    """
    __tablename__ = "referral_payout_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    amount_usd = Column(Numeric(14, 2), nullable=False)
    address = Column(String, nullable=False)       # TRC20 USDT
    status = Column(String, nullable=False, default="pending")  # pending | paid | rejected
    note = Column(String, nullable=True)           # admin's optional rejection reason / payout tx hash
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    resolved_at = Column(DateTime, nullable=True)

    user = relationship("User", foreign_keys=[user_id], back_populates="referral_payouts")


class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    wallet_type = Column(String, nullable=False)   # exchange | chain | perpdex
    type_value = Column(String, nullable=False)    # binance | tron | hyperliquid
    credentials = Column(JSON, nullable=True)      # encrypted {api_key, api_secret, ...} or {address}
    is_archived = Column(Boolean, nullable=False, default=False)
    can_trade = Column(Boolean, nullable=False, default=False)  # legacy — mirrors purpose='screener'
    purpose = Column(String, nullable=False, default="portfolio")  # 'portfolio' (read-only) | 'screener' (trading)
    is_main = Column(Boolean, nullable=False, default=False)  # main trading key for the venue (one per (user, venue))
    created_at = Column(DateTime, default=datetime.utcnow)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    user = relationship("User", back_populates="wallets")
    tags = relationship("Tag", secondary=wallet_tags, back_populates="wallets", lazy="joined")
    addresses = relationship("WalletAddress", back_populates="wallet", cascade="all, delete-orphan", lazy="joined")


class WalletAddress(Base):
    """Named addresses attached to exchange wallets."""
    __tablename__ = "wallet_addresses"

    id = Column(Integer, primary_key=True, index=True)
    wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    address = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    wallet = relationship("Wallet", back_populates="addresses")


class BalanceSnapshot(Base):
    """Last known balance per wallet — used for PnL calculation."""
    __tablename__ = "balance_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False, unique=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    totals = Column(JSON, nullable=False)         # {"USDT": "1234.56", "BTC": "0.5"}
    stable_total = Column(Float, nullable=False, default=0.0)  # pre-computed USD stable sum
    snapshot_at = Column(DateTime, default=datetime.utcnow)


class ProviderErrorLog(Base):
    """One row per failed provider fetch — used for error analytics."""
    __tablename__ = "provider_error_logs"

    id = Column(Integer, primary_key=True, index=True)
    wallet_type = Column(String, nullable=False)   # exchange | chain | perpdex
    type_value  = Column(String, nullable=False)   # binance | ethereum | hyperliquid
    error_type  = Column(String, nullable=False)   # rate_limit | auth | network | unknown
    created_at  = Column(DateTime, default=datetime.utcnow, index=True)


class BalanceHistory(Base):
    """Aggregate USD snapshot for Owner-tagged wallets — used for the portfolio chart."""
    __tablename__ = "balance_history"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    usd_total   = Column(Float,   nullable=False)
    # Per-asset USD breakdown at snapshot time, e.g. {"BTC": 3421.55, "ETH": 812.10}.
    # Used by the profile chart tooltip to show composition at the hovered point.
    # Nullable — older rows (pre-migration p2q3r4s5t6u7) stay without this data
    # and the tooltip falls back to the aggregate only.
    totals      = Column(JSON,    nullable=True)
    snapshot_at = Column(DateTime, default=datetime.utcnow, index=True)


class ArbAlert(Base):
    """Arbitrage spread alert — triggers Telegram message when spread threshold is crossed."""
    __tablename__ = "arb_alerts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String, nullable=False)
    long_exchange = Column(String, nullable=False)
    short_exchange = Column(String, nullable=False)
    threshold = Column(Float, nullable=False)          # min spread % to trigger
    direction = Column(String, nullable=False, default="any")  # any | above | below
    mode = Column(String, nullable=True)               # futures | spot | dex | dex_spot (null = futures)
    trigger_mode = Column(String, nullable=True)       # speed | protected (null = speed)
    enabled = Column(Boolean, nullable=False, default=True)
    last_triggered_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="arb_alerts")


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("name", "user_id", name="uq_tag_name_user"),
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    color = Column(String, nullable=False, default="#6366f1")
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)  # NULL = system tag

    wallets = relationship("Wallet", secondary=wallet_tags, back_populates="tags", lazy="joined")


class PaperPosition(Base):
    """Simulated arb position with live P&L tracking."""
    __tablename__ = "paper_positions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String, nullable=False)
    long_exchange = Column(String, nullable=False)
    short_exchange = Column(String, nullable=False)
    size_usd = Column(Float, nullable=False)
    entry_long_price = Column(Float, nullable=False)
    entry_short_price = Column(Float, nullable=False)
    entry_spread_pct = Column(Float, nullable=False)
    entry_fees_usd = Column(Float, nullable=False, default=0.0)
    accrued_funding_usd = Column(Float, nullable=False, default=0.0)
    status = Column(String, nullable=False, default="open")  # open | closed
    opened_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    closed_at = Column(DateTime, nullable=True)
    exit_spread_pct = Column(Float, nullable=True)
    realized_pnl_usd = Column(Float, nullable=True)
    last_updated = Column(DateTime, default=datetime.utcnow, nullable=False)


class OpportunitySnapshot(Base):
    """Minute-granularity snapshots of arb opportunities for historical replay + correlation."""
    __tablename__ = "opportunity_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, nullable=False, index=True)
    long_exchange = Column(String, nullable=False)
    short_exchange = Column(String, nullable=False)
    gross_funding = Column(Float, nullable=False)
    price_spread = Column(Float, nullable=False)
    net_profit = Column(Float, nullable=False)
    long_rate = Column(Float, nullable=False)
    short_rate = Column(Float, nullable=False)
    long_volume = Column(Float, nullable=False, default=0.0)
    short_volume = Column(Float, nullable=False, default=0.0)
    alpha_score = Column(Float, nullable=True)
    snapshot_at = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)


class ExchangeHealth(Base):
    """Rolling latency + availability measurements per exchange."""
    __tablename__ = "exchange_health"

    id = Column(Integer, primary_key=True, index=True)
    exchange = Column(String, nullable=False, index=True)
    ts = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)
    latency_ms = Column(Integer, nullable=False)
    ok = Column(Boolean, nullable=False)
    error = Column(String, nullable=True)


class AnomalyEvent(Base):
    """Detected spread anomaly (z-score outlier)."""
    __tablename__ = "anomaly_events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    symbol = Column(String, nullable=False)
    long_exchange = Column(String, nullable=False)
    short_exchange = Column(String, nullable=False)
    spread_pct = Column(Float, nullable=False)
    z_score = Column(Float, nullable=False)
    mean_pct = Column(Float, nullable=False)
    std_pct = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)


class TgLinkToken(Base):
    """Short-lived one-use token for linking Telegram to an Avalant user.
    Issued when the logged-in user clicks "Link Telegram" on /profile; consumed
    by the bot when the user taps /start link-<token>. Stored hashed at rest."""
    __tablename__ = "tg_link_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class WatchlistItem(Base):
    """User's saved pair for quick access."""
    __tablename__ = "watchlist_items"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String, nullable=False)
    long_exchange = Column(String, nullable=False)
    short_exchange = Column(String, nullable=False)
    note = Column(String, nullable=True)
    initial_spread_pct = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AppSetting(Base):
    """Global admin-tunable knobs (hidden tokens, disabled exchanges, etc.)."""
    __tablename__ = "app_settings"

    key = Column(String, primary_key=True)
    value = Column(JSON, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class PasswordResetToken(Base):
    """One row per password-reset link. token_hash stores SHA-256 of the raw
    token so a DB dump doesn't leak usable reset links. TTL-based cleanup is
    implicit — any row with expires_at < now() or used_at set is rejected."""
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class EmailVerifyToken(Base):
    """Mirror of PasswordResetToken for email-address verification at
    registration. Separate table so retention / TTL policies can evolve
    independently (verify tokens live 24h, reset tokens 15m)."""
    __tablename__ = "email_verify_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ── Pricing / monetisation ──────────────────────────────────────────────
class Plan(Base):
    """Admin-editable subscription plan. All limits and pricing live here so
    they can change without a redeploy. `features.perks` and `features.limits`
    are arbitrary string lists rendered on the pricing page."""
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True)
    slug = Column(String, nullable=False, unique=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    price_usd_monthly = Column(Numeric(10, 2), nullable=False, default=0)
    price_usd_annual = Column(Numeric(10, 2), nullable=False, default=0)
    portfolio_limit = Column(Integer, nullable=False, default=5)
    portfolio_limit_grace = Column(Integer, nullable=False, default=5)
    exchange_keys_per_venue = Column(Integer, nullable=False, default=1)
    trade_delay_ms = Column(Integer, nullable=False, default=0)
    has_portfolio = Column(Boolean, nullable=False, default=True)
    is_subscription = Column(Boolean, nullable=False, default=True)
    is_admin_only = Column(Boolean, nullable=False, default=False)
    features = Column(JSON, nullable=True)
    is_free = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class BillingPeriod(Base):
    """Admin-tunable billing-period catalogue. Each row is one
    commitment length (1, 3, 6, 12 months) with a discount %. Changing
    the discount column updates everyone's checkout flow without a
    deploy."""
    __tablename__ = "billing_periods"

    id = Column(Integer, primary_key=True)
    slug = Column(String, nullable=False, unique=True, index=True)
    label = Column(String, nullable=False)
    months = Column(Integer, nullable=False)
    discount_pct = Column(Numeric(5, 2), nullable=False, default=0)
    sort_order = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class PromoCode(Base):
    """Admin-managed discount codes. `applies_to_plan_ids` = JSON list of
    plan ids; null/empty list means 'every paid plan'. `discount_pct` is a
    Numeric(5,2) — frontend renders rounded to 2 decimals already, but the
    final cart amount is computed server-side to keep the discount honest."""
    __tablename__ = "promo_codes"

    id = Column(Integer, primary_key=True)
    code = Column(String, nullable=False, unique=True, index=True)
    discount_pct = Column(Numeric(5, 2), nullable=False, default=0)
    # Bonus subscription days — added to activated_until on payment activation.
    # A promo may have discount=0 + bonus_days>0 (pure bonus) or any combo.
    bonus_days = Column(Integer, nullable=False, default=0)
    max_uses = Column(Integer, nullable=True)
    used_count = Column(Integer, nullable=False, default=0)
    # null = unlimited per user (legacy); 1 = "once per user", 2+ = capped.
    # Enforced against PromoCodeUsage at validate time.
    per_user_max_uses = Column(Integer, nullable=True)
    # null = anyone with the code can redeem; set = ONLY this user. Useful
    # for one-off comp grants where the code is shared but only one
    # specific account should be allowed to use it.
    target_user_id = Column(Integer,
                            ForeignKey("users.id", ondelete="SET NULL"),
                            nullable=True, index=True)
    applies_to_plan_ids = Column(JSON, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Payment(Base):
    """Single CryptoCloud invoice lifecycle row. Created at /checkout, moved
    to status='paid' by the webhook, then `activated_until` is computed and
    the user's plan_id flipped. Failed/expired invoices stay in the table
    for the audit trail."""
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer,
                     ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    plan_id = Column(Integer,
                     ForeignKey("plans.id", ondelete="RESTRICT"),
                     nullable=False)
    billing_cycle = Column(String, nullable=True)             # legacy free-text mirror; new flows use billing_period_id
    billing_period_id = Column(Integer,
                               ForeignKey("billing_periods.id", ondelete="RESTRICT"),
                               nullable=True)
    base_amount_usd = Column(Numeric(10, 2), nullable=False)
    discount_pct = Column(Numeric(5, 2), nullable=False, default=0)
    final_amount_usd = Column(Numeric(10, 2), nullable=False)
    promo_code_id = Column(Integer,
                           ForeignKey("promo_codes.id", ondelete="SET NULL"),
                           nullable=True)
    provider = Column(String, nullable=False, default="cryptocloud")
    provider_invoice_id = Column(String, nullable=True, unique=True, index=True)
    provider_invoice_url = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending", index=True)  # pending | paid | failed | expired | refunded
    paid_at = Column(DateTime, nullable=True)
    activated_until = Column(DateTime, nullable=True)
    # Refund bookkeeping. Stamped by admin via /admin/payments/{id}/refund
    # or by the webhook on a `refunded` / `chargeback` event. Once set,
    # `_activate_user` refuses to re-process this payment so a replayed
    # success webhook can't reverse the refund.
    refunded_at = Column(DateTime, nullable=True)
    refunded_reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class PromoCodeUsage(Base):
    """Append-only ledger — one row per successful checkout that used a
    promo. Powers the per-promo stats endpoint (count, total revenue,
    avg discount). Never delete — even after the promo itself is removed
    we want the historic numbers."""
    __tablename__ = "promo_code_usages"

    id = Column(Integer, primary_key=True)
    promo_code_id = Column(Integer,
                           ForeignKey("promo_codes.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    user_id = Column(Integer,
                     ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    payment_id = Column(Integer,
                        ForeignKey("payments.id", ondelete="CASCADE"),
                        nullable=False)
    plan_id = Column(Integer,
                     ForeignKey("plans.id", ondelete="RESTRICT"),
                     nullable=False)
    discount_pct = Column(Numeric(5, 2), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Popup(Base):
    """Admin-defined promotion popup. `target_type` controls audience (all
    or single user); `frequency_type` controls re-show cadence after a
    dismiss (`once` = forever, `every_n_min` = wait `frequency_minutes`
    after dismiss before re-eligibility)."""
    __tablename__ = "popups"

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    body = Column(String, nullable=False)
    button_text = Column(String, nullable=False, default="View pricing")
    button_url = Column(String, nullable=False, default="/pricing")
    target_type = Column(String, nullable=False, default="all")          # "all" | "user"
    target_user_id = Column(Integer,
                            ForeignKey("users.id", ondelete="CASCADE"),
                            nullable=True, index=True)
    frequency_type = Column(String, nullable=False, default="once")      # "once" | "every_n_min"
    frequency_minutes = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class AuditLogEntry(Base):
    """Append-only ledger of destructive admin / billing actions. Never
    UPDATE, never DELETE — incident-response truth source."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    actor_user_id = Column(Integer,
                           ForeignKey("users.id", ondelete="SET NULL"),
                           nullable=True, index=True)
    actor_ip = Column(String, nullable=True)
    actor_user_agent = Column(String, nullable=True)
    action = Column(String, nullable=False, index=True)
    target_type = Column(String, nullable=True, index=True)
    target_id = Column(Integer, nullable=True)
    delta = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class TradeOrder(Base):
    """Append-only log of every order our service sent to a venue.

    Captures both successes and failures with the raw exchange response so
    the Order History tab can show the user exactly what happened (and why
    it failed, if it did). Errors are bucketed via `error_kind` so the UI
    can sanitize internal errors into "unexpected error" while showing the
    venue's actual message for exchange-side errors.
    """
    __tablename__ = "trade_orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="SET NULL"), nullable=True, index=True)
    position_id = Column(Integer, ForeignKey("trade_positions.id", ondelete="SET NULL"), nullable=True, index=True)
    exchange = Column(String, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)              # buy | sell
    intent = Column(String, nullable=False, index=True)  # open | close
    order_type = Column(String, nullable=False, default="market")
    requested_qty = Column(Float, nullable=False)
    requested_price = Column(Float, nullable=True)
    leverage = Column(Integer, nullable=True)
    margin_mode = Column(String, nullable=True)
    status = Column(String, nullable=False, index=True)  # pending | filled | partial | failed | canceled
    exchange_order_id = Column(String, nullable=True, index=True)
    filled_qty = Column(Float, nullable=True)
    avg_fill_price = Column(Float, nullable=True)
    fee_usd = Column(Float, nullable=True)
    error_code = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    error_kind = Column(String, nullable=True)         # exchange | internal | user
    raw_response = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    finalized_at = Column(DateTime, nullable=True)


class TradePosition(Base):
    """Lifecycle of an open or closed trading position.

    Single-leg or pair (long/short or spot/short). Pairs are stitched
    together either by the auto-detector (same symbol, opposite sides,
    notional within spread%±5%, opened within 5 min) or by the user's
    Sync ⇆ button. P&L tab reads `kind=pair, status=closed` rows;
    `single` rows are shown as one-sided P&L.
    """
    __tablename__ = "trade_positions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String, nullable=False)              # single | pair
    pair_kind = Column(String, nullable=True)          # long_short | spot_short | dex_spot | null
    status = Column(String, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)

    leg_a_wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="SET NULL"), nullable=True)
    leg_a_exchange = Column(String, nullable=False)
    leg_a_side = Column(String, nullable=False)
    leg_a_qty = Column(Float, nullable=False)
    leg_a_entry_price = Column(Float, nullable=True)
    leg_a_exit_price = Column(Float, nullable=True)
    leg_a_realized_pnl_usd = Column(Float, nullable=True)
    leg_a_funding_pnl_usd = Column(Float, nullable=True)
    leg_a_fees_usd = Column(Float, nullable=True)
    leg_a_open_order_id = Column(Integer, ForeignKey("trade_orders.id", ondelete="SET NULL"), nullable=True)
    leg_a_close_order_id = Column(Integer, ForeignKey("trade_orders.id", ondelete="SET NULL"), nullable=True)
    # 'futures' (default) or 'spot'. Set explicitly during fills-backfill so
    # the spot/short auto-pair logic can match a closed spot LONG with a
    # closed futures SHORT on the same symbol/window.
    leg_a_market = Column(String, nullable=False, default="futures")

    leg_b_wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="SET NULL"), nullable=True)
    leg_b_exchange = Column(String, nullable=True)
    leg_b_side = Column(String, nullable=True)
    leg_b_qty = Column(Float, nullable=True)
    leg_b_entry_price = Column(Float, nullable=True)
    leg_b_exit_price = Column(Float, nullable=True)
    leg_b_realized_pnl_usd = Column(Float, nullable=True)
    leg_b_funding_pnl_usd = Column(Float, nullable=True)
    leg_b_fees_usd = Column(Float, nullable=True)
    leg_b_open_order_id = Column(Integer, ForeignKey("trade_orders.id", ondelete="SET NULL"), nullable=True)
    leg_b_close_order_id = Column(Integer, ForeignKey("trade_orders.id", ondelete="SET NULL"), nullable=True)
    leg_b_market = Column(String, nullable=True)

    # 'platform' (we placed the order), 'reconcile' (we saw it via the live
    # positions endpoint, then noticed it was gone), or 'fills_backfill'
    # (reconstructed from trade_fills). Lets the UI tag rows and helps the
    # backfill service avoid re-creating rows that already exist.
    source = Column(String, nullable=False, default="platform")
    realized_pnl_usd = Column(Float, nullable=True)
    entry_spread_pct = Column(Float, nullable=True)
    exit_spread_pct = Column(Float, nullable=True)
    opened_externally = Column(Boolean, nullable=False, default=False)
    closed_externally = Column(Boolean, nullable=False, default=False)
    opened_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    closed_at = Column(DateTime, nullable=True, index=True)

    # Wraps execution legs into a user-intent ArbPosition. Set on auto-pair
    # detection or when a trigger fires via Live Trading panel. Null for
    # legacy single-leg trades that haven't been paired yet.
    arb_position_id = Column(Integer, ForeignKey("arb_positions.id", ondelete="SET NULL"), nullable=True, index=True)


class TradePairDecision(Base):
    """User's persisted choice about whether two single positions should be
    paired or not. Survives page refresh so we don't undo the user's manual
    Sync ⇆ / Unpair on every reload.

    `leg_*_key` is a stable fingerprint per-leg (wallet+symbol+side+rounded
    entry price) — we don't rely on the venue's position id since some of
    them recycle.
    """
    __tablename__ = "trade_pair_decisions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    leg_a_key = Column(String, nullable=False)
    leg_b_key = Column(String, nullable=False)
    decision = Column(String, nullable=False)  # paired | unpaired
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "leg_a_key", "leg_b_key", name="uq_pair_decisions_user_legs"),
    )


class TradeFill(Base):
    """Raw venue fill row.

    One row per execution returned by the venue's fills/trade-history API.
    Backfilled by `fills_backfill_service` on user demand (PnL tab Sync
    button) and used to reconstruct closed `trade_positions` rows.
    Idempotent re-syncs are safe via UNIQUE on
    (wallet_id, exchange, market, ext_trade_id)."""
    __tablename__ = "trade_fills"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="SET NULL"),
                       nullable=True, index=True)
    exchange = Column(String, nullable=False)
    market = Column(String, nullable=False)  # 'futures' | 'spot'
    # 'trade' = actual order fill, 'funding' = periodic funding settlement.
    kind = Column(String, nullable=False, default="trade")
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=True)     # 'buy' | 'sell' | null for funding
    qty = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    fee_usd = Column(Float, nullable=True)
    realized_pnl_usd = Column(Float, nullable=True)
    ts = Column(DateTime, nullable=False)
    ext_trade_id = Column(String, nullable=False)
    ext_order_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("wallet_id", "exchange", "market", "kind", "ext_trade_id",
                         name="uq_trade_fills_dedup"),
    )


class FillsSyncCursor(Base):
    """High-watermark for delta pulls of `trade_fills`. Per
    (wallet, exchange, market) we remember the last fill ts we ingested
    and the last time the sync ran. The next pull asks for fills strictly
    > last_ts so re-syncs cost ~one HTTP round-trip per venue when the
    user hasn't traded."""
    __tablename__ = "fills_sync_cursor"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="CASCADE"),
                       nullable=False)
    exchange = Column(String, nullable=False)
    market = Column(String, nullable=False)
    last_ts = Column(DateTime, nullable=True)
    last_synced_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("wallet_id", "exchange", "market",
                         name="uq_fills_sync_cursor_key"),
    )


class PopupDismissal(Base):
    """Per-user dismissal log. `dismissed_at` lets the popup_service decide
    whether `every_n_min` cadence has elapsed since the last close. Unique
    on (popup_id, user_id) so we always update the timestamp instead of
    accumulating rows."""
    __tablename__ = "popup_dismissals"

    id = Column(Integer, primary_key=True)
    popup_id = Column(Integer,
                      ForeignKey("popups.id", ondelete="CASCADE"),
                      nullable=False)
    user_id = Column(Integer,
                     ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    dismissed_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("popup_id", "user_id", name="uq_popup_dismissals_user_popup"),
    )


class ArbPosition(Base):
    """User-intent arbitrage pair entity. Wraps 1..N execution legs
    (TradePosition rows linked via arb_position_id) plus 0..N child
    triggers (ArbTriggerOrder.parent_trigger_id chain).

    Lifecycle: pending → opening → open → (closing → closed | partial).
    Status is terminal at 'closed' / 'cancelled'. See DEV_PROMPT.md §7.6.
    """
    __tablename__ = "arb_positions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    kind = Column(String, nullable=False)              # long_short | spot_short
    long_exchange = Column(String, nullable=False)
    long_symbol = Column(String, nullable=False)
    long_wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="SET NULL"), nullable=True)
    short_exchange = Column(String, nullable=False)
    short_symbol = Column(String, nullable=False)
    short_wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="SET NULL"), nullable=True)

    target_qty_token = Column(Float, nullable=True)
    leverage = Column(Integer, nullable=True)
    margin_mode = Column(String, nullable=False, default="isolated")

    entry_spread_pct = Column(Float, nullable=True)
    long_entry_price = Column(Float, nullable=True)
    short_entry_price = Column(Float, nullable=True)
    long_qty = Column(Float, nullable=False, default=0.0)
    short_qty = Column(Float, nullable=False, default=0.0)
    opened_at = Column(DateTime, nullable=True)

    exit_spread_pct = Column(Float, nullable=True)
    long_exit_price = Column(Float, nullable=True)
    short_exit_price = Column(Float, nullable=True)
    realized_pnl_usd = Column(Float, nullable=True)
    closed_at = Column(DateTime, nullable=True)

    status = Column(String, nullable=False, default="pending", index=True)
    synced_externally = Column(Boolean, nullable=False, default=False)
    closed_externally = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    triggers = relationship("ArbTriggerOrder", back_populates="arb_position",
                            foreign_keys="ArbTriggerOrder.arb_position_id",
                            cascade="all, delete-orphan")


class ArbTriggerOrder(Base):
    """Server-side conditional order for arb pairs. Supports portion-based
    fills, infinite-fill loops, scheduled activation, and parent/child
    cascading (parent open → linked TP/SL children).

    State machine: scheduled → pending → firing → fired | failed | cancelled.
    Atomic claim-on-fire SQL prevents cross-replica double-fires.
    """
    __tablename__ = "arb_trigger_orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    arb_position_id = Column(Integer, ForeignKey("arb_positions.id", ondelete="CASCADE"), nullable=True, index=True)
    parent_trigger_id = Column(Integer, ForeignKey("arb_trigger_orders.id", ondelete="CASCADE"), nullable=True, index=True)

    kind = Column(String, nullable=False)              # open | close | tp | sl
    trigger_spread_pct = Column(Float, nullable=True)  # null = market

    long_exchange = Column(String, nullable=True)
    long_symbol = Column(String, nullable=True)
    long_wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="SET NULL"), nullable=True)
    short_exchange = Column(String, nullable=True)
    short_symbol = Column(String, nullable=True)
    short_wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="SET NULL"), nullable=True)

    total_qty_token = Column(Float, nullable=True)
    portion_size_token = Column(Float, nullable=True)
    portions_filled = Column(Integer, nullable=False, default=0)
    portions_target = Column(Integer, nullable=True)
    infinite_fill = Column(Boolean, nullable=False, default=False)
    activate_at = Column(DateTime, nullable=True)

    leverage = Column(Integer, nullable=True)
    margin_mode = Column(String, nullable=False, default="isolated")
    reduce_only = Column(Boolean, nullable=False, default=False)

    status = Column(String, nullable=False, default="pending", index=True)
    last_fired_at = Column(DateTime, nullable=True)
    error_kind = Column(String, nullable=True)
    error_message = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    arb_position = relationship("ArbPosition", back_populates="triggers", foreign_keys=[arb_position_id])
    parent = relationship("ArbTriggerOrder", remote_side="ArbTriggerOrder.id",
                          foreign_keys=[parent_trigger_id], back_populates="children")
    children = relationship("ArbTriggerOrder", back_populates="parent",
                            foreign_keys=[parent_trigger_id], cascade="all, delete-orphan")


# ── Arb spread time-series (chart history) ─────────────────────────────
# Three tier OHLC tables for the in/out spread chart, populated by the
# go-fetcher → Redis stream → consumer pipeline. Schema mirrors the
# alembic migration n3o4p5q6r7s8 — keep in sync.

class _ArbSpreadCandleBase:
    """Mixin: PK + OHLC columns shared by the 5s/1m/1h tables."""
    exchange_long = Column(String(16), primary_key=True)
    exchange_short = Column(String(16), primary_key=True)
    symbol = Column(String(32), primary_key=True)
    bucket_ts = Column(Integer, primary_key=True)
    in_open = Column(Float, nullable=False)
    in_high = Column(Float, nullable=False)
    in_low = Column(Float, nullable=False)
    in_close = Column(Float, nullable=False)
    out_open = Column(Float, nullable=False)
    out_high = Column(Float, nullable=False)
    out_low = Column(Float, nullable=False)
    out_close = Column(Float, nullable=False)
    samples = Column(SmallInteger, nullable=False, default=1)


class ArbSpreadCandle5s(_ArbSpreadCandleBase, Base):
    """5s OHLC of in/out spread per (long_ex, short_ex, symbol). Written
    by go-fetcher via Redis stream → consumer. Retention 24h."""
    __tablename__ = "arb_spread_candles_5s"


class ArbSpreadCandle1m(_ArbSpreadCandleBase, Base):
    """1m rollup from 5s. Built by rollup daemon hourly. Retention 7d."""
    __tablename__ = "arb_spread_candles_1m"


class ArbSpreadCandle1h(_ArbSpreadCandleBase, Base):
    """1h rollup from 1m. Built by rollup daemon daily. Retention 90d."""
    __tablename__ = "arb_spread_candles_1h"


# ── Split-discount referral codes (new system, replaces fixed-20% accrual) ───

class ReferralCode(Base):
    """User-owned discount + commission code.

    The CHECK constraints in __table_args__ are the LOAD-BEARING security
    invariants of the split model. They are duplicated in alembic
    r1s2t3u4v5w6 so prod and dev/test (Base.metadata.create_all) carry
    the same defenses. NEVER weaken either side independently.

    Invariants:
      1. commission_pct >= 0 AND discount_pct >= 0
      2. commission_pct + discount_pct <= 45   — global cap (any type)
      3. created_by_admin_id IS NOT NULL OR sum <= 25 — high pool needs admin
      4. code_type IN ('self_serve','admin')
      5. type ↔ admin_id consistency (closes forged-label bypass)

    Immutability: there is no service path that updates commission_pct /
    discount_pct / code_type after INSERT. Codes are not deleted (audit +
    already-bound referees would be orphaned). Saturation via the
    15-registration cap is the natural lifecycle.
    """
    __tablename__ = "referral_codes"
    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    code = Column(String(32), nullable=False)
    commission_pct = Column(Numeric(5, 2), nullable=False)
    discount_pct = Column(Numeric(5, 2), nullable=False)
    code_type = Column(String(16), nullable=False)
    # NULL for self_serve. For admin codes, the FK is set to the admin
    # user.id at create time so audit can trace who issued a high-pool code.
    created_by_admin_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                                 nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            'commission_pct >= 0 AND discount_pct >= 0',
            name='ck_referral_codes_nonneg',
        ),
        CheckConstraint(
            'commission_pct + discount_pct <= 45',
            name='ck_referral_codes_total_cap',
        ),
        CheckConstraint(
            'created_by_admin_id IS NOT NULL '
            'OR (commission_pct + discount_pct <= 25)',
            name='ck_referral_codes_high_pool_needs_admin',
        ),
        CheckConstraint(
            "code_type IN ('self_serve', 'admin')",
            name='ck_referral_codes_type_enum',
        ),
        CheckConstraint(
            "(code_type = 'self_serve' AND created_by_admin_id IS NULL) "
            "OR (code_type = 'admin' AND created_by_admin_id IS NOT NULL)",
            name='ck_referral_codes_type_admin_match',
        ),
        # Case-insensitive uniqueness — LOWER(code) UNIQUE. Crypto/crypto
        # collisions blocked. Functional index works on both Postgres and
        # SQLite (since 3.9 — well below our pinned version).
        Index('uq_referral_codes_lower', text('LOWER(code)'), unique=True),
    )

    owner = relationship("User", foreign_keys=[owner_id])
    created_by = relationship("User", foreign_keys=[created_by_admin_id])


class ReferralCodeRegistration(Base):
    """Binds a referee to ONE code, forever. UNIQUE(referee_id) is the
    load-bearing anti-reattribution invariant — a user cannot switch
    codes post-signup, nor be bound to a second one. The 15-registration
    cap is enforced at the service layer (counting rows by code_id)
    rather than as a CHECK because dynamic counts can't be expressed in
    CHECK; the UNIQUE on referee_id covers the harder case (a single
    user owned by two referrers).

    Duplicated in alembic r1s2t3u4v5w6.
    """
    __tablename__ = "referral_code_registrations"
    id = Column(Integer, primary_key=True, index=True)
    code_id = Column(Integer, ForeignKey("referral_codes.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    referee_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint('referee_id', name='uq_reg_referee'),
        UniqueConstraint('code_id', 'referee_id', name='uq_reg_code_referee'),
    )

    code = relationship("ReferralCode")
    referee = relationship("User", foreign_keys=[referee_id])


class ReferralCodeUsage(Base):
    """Append-only ledger of paid invoices that consumed a code's discount
    and produced a commission earning. UNIQUE(payment_id) is the
    idempotency seal — webhook retry or double-call cannot double-credit.

    reversed_at is set when the linked payment is refunded — the
    5-per-referee counter excludes reversed rows so the referee gets
    their discount + commission slot back.

    Paired with ReferralEarning: every non-reversed Usage row has a
    matching Earning row (same payment_id). Earning is the balance ledger
    (powers payouts); Usage is the cap ledger (powers the 5-per-referee
    counter + the discount history for the receipt UI).
    """
    __tablename__ = "referral_code_usages"
    id = Column(Integer, primary_key=True, index=True)
    code_id = Column(Integer, ForeignKey("referral_codes.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    referee_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    payment_id = Column(Integer, ForeignKey("payments.id", ondelete="SET NULL"),
                        nullable=True, unique=True)
    payment_amount_usd = Column(Numeric(14, 2), nullable=False)
    commission_earned = Column(Numeric(14, 2), nullable=False)
    discount_applied = Column(Numeric(14, 2), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    reversed_at = Column(DateTime, nullable=True)
    reversal_reason = Column(String, nullable=True)

    code = relationship("ReferralCode")
    referee = relationship("User", foreign_keys=[referee_id])
