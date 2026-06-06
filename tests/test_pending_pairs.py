"""Phase 4.3 — partially-closed pair surfacing.

trade_service.list_user_pnl_pending_pairs returns pairs where ONE leg
has closed and the counterpart is still open. The PNL list filters
these out (correct: incomplete result), and the Pending section uses
this function to show 'pair pending close: X of 2 legs'.

Same _pnl_can_pair rule (12% notional tolerance, 5-min open window,
respects TradePairDecision unpair overrides).
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-32-chars-long-aaaa")

from datetime import datetime, timedelta


def _in_memory_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from backend.db.base import Base
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _make_user(db, name="alice"):
    from backend.db.models import User
    u = User(username=name, email=f"{name}@t.local", hashed_password="x", plan="free")
    db.add(u); db.commit(); db.refresh(u)
    return u


def _make_pos(db, user, ex, sym, side, qty, entry, status, opened_at, closed_at=None,
              realized_pnl=None):
    from backend.db.models import TradePosition
    p = TradePosition(
        user_id=user.id, kind="single", status=status, symbol=sym,
        leg_a_exchange=ex, leg_a_side=side,
        leg_a_qty=qty, leg_a_entry_price=entry,
        leg_a_realized_pnl_usd=realized_pnl, realized_pnl_usd=realized_pnl,
        leg_a_market="futures",
        opened_at=opened_at, closed_at=closed_at,
        source="platform",
    )
    db.add(p); db.commit(); db.refresh(p)
    return p


# ── Partial pair: closed LONG + open SHORT → in pending list ──────────
def test_partial_pair_surfaced():
    from backend.services.trade_service import list_user_pnl_pending_pairs
    db = _in_memory_db()
    user = _make_user(db)
    now = datetime.utcnow()
    # Closed long (1 min after open)
    _make_pos(db, user, "binance", "BTC", "buy", 0.1, 55000,
              status="closed", opened_at=now - timedelta(minutes=10),
              closed_at=now - timedelta(minutes=9), realized_pnl=500.0)
    # Open short, opened ~same time (within 5min)
    _make_pos(db, user, "bybit", "BTC", "sell", 0.1, 56000,
              status="open", opened_at=now - timedelta(minutes=10))

    pending = list_user_pnl_pending_pairs(db, user.id)
    assert len(pending) == 1
    assert pending[0]["status"] == "partially_closed"
    assert pending[0]["legs_closed"] == 1
    assert pending[0]["symbol"] == "BTC"
    assert pending[0]["closed_leg"]["exchange"] == "binance"
    assert pending[0]["open_leg"]["exchange"] == "bybit"
    assert pending[0]["closed_leg"]["realized_pnl_usd"] == 500.0


# ── Both closed → NOT in pending (in main PNL list) ───────────────────
def test_both_closed_not_pending():
    from backend.services.trade_service import list_user_pnl_pending_pairs
    db = _in_memory_db()
    user = _make_user(db)
    now = datetime.utcnow()
    _make_pos(db, user, "binance", "BTC", "buy", 0.1, 55000,
              status="closed", opened_at=now - timedelta(minutes=10),
              closed_at=now - timedelta(minutes=9), realized_pnl=500.0)
    _make_pos(db, user, "bybit", "BTC", "sell", 0.1, 56000,
              status="closed", opened_at=now - timedelta(minutes=10),
              closed_at=now - timedelta(minutes=8), realized_pnl=-400.0)

    pending = list_user_pnl_pending_pairs(db, user.id)
    assert pending == []


# ── Both open → NOT in pending (no closed leg yet) ────────────────────
def test_both_open_not_pending():
    from backend.services.trade_service import list_user_pnl_pending_pairs
    db = _in_memory_db()
    user = _make_user(db)
    now = datetime.utcnow()
    _make_pos(db, user, "binance", "BTC", "buy", 0.1, 55000,
              status="open", opened_at=now - timedelta(minutes=5))
    _make_pos(db, user, "bybit", "BTC", "sell", 0.1, 56000,
              status="open", opened_at=now - timedelta(minutes=5))
    pending = list_user_pnl_pending_pairs(db, user.id)
    assert pending == []


# ── Closed leg only, no counterpart → NOT in pending ──────────────────
def test_closed_single_not_pending():
    from backend.services.trade_service import list_user_pnl_pending_pairs
    db = _in_memory_db()
    user = _make_user(db)
    now = datetime.utcnow()
    _make_pos(db, user, "binance", "BTC", "buy", 0.1, 55000,
              status="closed", opened_at=now - timedelta(minutes=10),
              closed_at=now - timedelta(minutes=9), realized_pnl=500.0)
    pending = list_user_pnl_pending_pairs(db, user.id)
    assert pending == []


# ── Different symbols never pair ──────────────────────────────────────
def test_different_symbols_no_pending():
    from backend.services.trade_service import list_user_pnl_pending_pairs
    db = _in_memory_db()
    user = _make_user(db)
    now = datetime.utcnow()
    _make_pos(db, user, "binance", "BTC", "buy", 0.1, 55000,
              status="closed", opened_at=now - timedelta(minutes=10),
              closed_at=now - timedelta(minutes=9), realized_pnl=500.0)
    _make_pos(db, user, "bybit", "ETH", "sell", 1.0, 2900,
              status="open", opened_at=now - timedelta(minutes=10))
    pending = list_user_pnl_pending_pairs(db, user.id)
    assert pending == []
