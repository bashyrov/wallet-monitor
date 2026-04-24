from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, Table, ForeignKey, JSON, Boolean, Float, Numeric, UniqueConstraint
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

    wallets = relationship("Wallet", back_populates="user", cascade="all, delete-orphan")
    arb_alerts = relationship("ArbAlert", back_populates="user", cascade="all, delete-orphan")


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
    features = Column(JSON, nullable=True)
    is_free = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    sort_order = Column(Integer, nullable=False, default=0)
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
    discount_pct = Column(Numeric(5, 2), nullable=False)
    max_uses = Column(Integer, nullable=True)
    used_count = Column(Integer, nullable=False, default=0)
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
    billing_cycle = Column(String, nullable=False)            # "monthly" | "annual"
    base_amount_usd = Column(Numeric(10, 2), nullable=False)
    discount_pct = Column(Numeric(5, 2), nullable=False, default=0)
    final_amount_usd = Column(Numeric(10, 2), nullable=False)
    promo_code_id = Column(Integer,
                           ForeignKey("promo_codes.id", ondelete="SET NULL"),
                           nullable=True)
    provider = Column(String, nullable=False, default="cryptocloud")
    provider_invoice_id = Column(String, nullable=True, unique=True, index=True)
    provider_invoice_url = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending", index=True)  # pending | paid | failed | expired
    paid_at = Column(DateTime, nullable=True)
    activated_until = Column(DateTime, nullable=True)
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
