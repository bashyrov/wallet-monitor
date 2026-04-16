from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, Table, ForeignKey, JSON, Boolean, Float, UniqueConstraint
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
    plan = Column(String, nullable=False, default="basic")  # basic | pro | platinum | enterprise | unlim
    plan_expires_at = Column(DateTime, nullable=True)
    request_count = Column(Integer, nullable=False, default=0)
    last_active_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    tg_username = Column(String, nullable=True)
    tg_chat_id = Column(Integer, nullable=True)   # filled after user runs /start to the bot

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


class WatchlistItem(Base):
    """User's saved pair for quick access."""
    __tablename__ = "watchlist_items"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String, nullable=False)
    long_exchange = Column(String, nullable=False)
    short_exchange = Column(String, nullable=False)
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
