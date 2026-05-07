"""auto_pair_internal_legs() — comprehensive coverage.

Spec (DEV_PROMPT.md §7.6.G2):
  same symbol_normalized
  + opposite side
  + different exchange
  + notional ±12%
  + opened within ±10 min

Anything opened through us must be 100% trackable without Sync.
"""
from datetime import datetime, timedelta


def _user(client, auth):
    from backend.db.base import SessionLocal
    from backend.db.models import User
    db = SessionLocal()
    try:
        return db.query(User).filter(User.username == "alice").first()
    finally:
        db.close()


def _mk_position(db, **kw):
    """Create an open single-leg TradePosition with sensible defaults."""
    from backend.db.models import TradePosition
    defaults = dict(
        kind="single", status="open", symbol="LAB",
        leg_a_wallet_id=1, leg_a_exchange="gate", leg_a_side="buy",
        leg_a_qty=1000, leg_a_entry_price=0.005,
        opened_at=datetime.utcnow(),
    )
    defaults.update(kw)
    p = TradePosition(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_pairs_two_opposing_legs(client, auth):
    """Long Gate + Short MEXC same symbol same notional → pair."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition
    from backend.services.trigger_order_service import auto_pair_internal_legs

    user = _user(client, auth)
    db = SessionLocal()
    try:
        _mk_position(db, user_id=user.id, leg_a_exchange="gate",  leg_a_side="buy",
                      leg_a_qty=1000, leg_a_entry_price=0.005)
        _mk_position(db, user_id=user.id, leg_a_exchange="mexc",  leg_a_side="sell",
                      leg_a_qty=1000, leg_a_entry_price=0.005)
        created = auto_pair_internal_legs(db, user.id)
        assert len(created) == 1
        ap = db.query(ArbPosition).filter(ArbPosition.user_id == user.id).first()
        assert ap is not None
        assert ap.kind == "long_short"
        assert ap.long_exchange == "gate"
        assert ap.short_exchange == "mexc"
        assert ap.synced_externally is False    # opened through us
    finally:
        db.close()


def test_does_not_pair_same_direction(client, auth):
    """Two BUY legs on different venues → not a pair."""
    from backend.db.base import SessionLocal
    from backend.services.trigger_order_service import auto_pair_internal_legs

    user = _user(client, auth)
    db = SessionLocal()
    try:
        _mk_position(db, user_id=user.id, leg_a_exchange="gate", leg_a_side="buy")
        _mk_position(db, user_id=user.id, leg_a_exchange="mexc", leg_a_side="buy")
        created = auto_pair_internal_legs(db, user.id)
        assert created == []
    finally:
        db.close()


def test_does_not_pair_same_exchange(client, auth):
    """Long + short on the SAME exchange → not a real arb pair."""
    from backend.db.base import SessionLocal
    from backend.services.trigger_order_service import auto_pair_internal_legs

    user = _user(client, auth)
    db = SessionLocal()
    try:
        _mk_position(db, user_id=user.id, leg_a_exchange="gate", leg_a_side="buy")
        _mk_position(db, user_id=user.id, leg_a_exchange="gate", leg_a_side="sell")
        created = auto_pair_internal_legs(db, user.id)
        assert created == []
    finally:
        db.close()


def test_does_not_pair_notional_outside_tolerance(client, auth):
    """1000 @ 0.005 vs 100 @ 0.005 — way more than ±12% off → no pair."""
    from backend.db.base import SessionLocal
    from backend.services.trigger_order_service import auto_pair_internal_legs

    user = _user(client, auth)
    db = SessionLocal()
    try:
        _mk_position(db, user_id=user.id, leg_a_exchange="gate", leg_a_side="buy",
                      leg_a_qty=1000, leg_a_entry_price=0.005)
        _mk_position(db, user_id=user.id, leg_a_exchange="mexc", leg_a_side="sell",
                      leg_a_qty=100, leg_a_entry_price=0.005)
        created = auto_pair_internal_legs(db, user.id)
        assert created == []
    finally:
        db.close()


def test_does_not_pair_outside_time_window(client, auth):
    """Legs opened > 10 min apart → not a pair (might be unrelated trades)."""
    from backend.db.base import SessionLocal
    from backend.services.trigger_order_service import auto_pair_internal_legs

    user = _user(client, auth)
    db = SessionLocal()
    try:
        long_time  = datetime.utcnow() - timedelta(minutes=20)
        short_time = datetime.utcnow()
        _mk_position(db, user_id=user.id, leg_a_exchange="gate", leg_a_side="buy",
                      opened_at=long_time)
        _mk_position(db, user_id=user.id, leg_a_exchange="mexc", leg_a_side="sell",
                      opened_at=short_time)
        created = auto_pair_internal_legs(db, user.id)
        assert created == []
    finally:
        db.close()


def test_skips_already_wrapped_legs(client, auth):
    """Legs that already have arb_position_id are not re-wrapped."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition
    from backend.services.trigger_order_service import auto_pair_internal_legs

    user = _user(client, auth)
    db = SessionLocal()
    try:
        # Pre-existing arb_position
        ap = ArbPosition(
            user_id=user.id, kind="long_short",
            long_exchange="gate", long_symbol="LAB", long_wallet_id=1,
            short_exchange="mexc", short_symbol="LAB", short_wallet_id=2,
            status="open", long_qty=1000, short_qty=1000,
        )
        db.add(ap)
        db.flush()
        # Wrapped legs
        _mk_position(db, user_id=user.id, leg_a_exchange="gate", leg_a_side="buy",
                      arb_position_id=ap.id, leg_a_wallet_id=1)
        _mk_position(db, user_id=user.id, leg_a_exchange="mexc", leg_a_side="sell",
                      arb_position_id=ap.id, leg_a_wallet_id=2)
        created = auto_pair_internal_legs(db, user.id)
        assert created == []     # already wrapped → skipped
        # Only one arb_position should exist (the original)
        cnt = db.query(ArbPosition).filter(ArbPosition.user_id == user.id).count()
        assert cnt == 1
    finally:
        db.close()


def test_pairs_only_unwrapped_legs_when_mixed(client, auth):
    """One wrapped pair + one unwrapped pair — the unwrapped one gets
    paired, the wrapped one is left alone."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition
    from backend.services.trigger_order_service import auto_pair_internal_legs

    user = _user(client, auth)
    db = SessionLocal()
    try:
        # Already-wrapped LAB pair
        ap = ArbPosition(
            user_id=user.id, kind="long_short",
            long_exchange="gate", long_symbol="LAB", long_wallet_id=1,
            short_exchange="mexc", short_symbol="LAB", short_wallet_id=2,
            status="open", long_qty=1000, short_qty=1000,
        )
        db.add(ap); db.flush()
        _mk_position(db, user_id=user.id, symbol="LAB", leg_a_exchange="gate", leg_a_side="buy",
                      arb_position_id=ap.id, leg_a_wallet_id=1)
        _mk_position(db, user_id=user.id, symbol="LAB", leg_a_exchange="mexc", leg_a_side="sell",
                      arb_position_id=ap.id, leg_a_wallet_id=2)
        # Unwrapped VANRY pair — opened through us, never wrapped yet
        _mk_position(db, user_id=user.id, symbol="VANRY",
                      leg_a_exchange="bybit", leg_a_side="buy",  leg_a_wallet_id=3)
        _mk_position(db, user_id=user.id, symbol="VANRY",
                      leg_a_exchange="binance", leg_a_side="sell", leg_a_wallet_id=4)

        created = auto_pair_internal_legs(db, user.id)
        assert len(created) == 1
        assert created[0].long_symbol == "VANRY"
    finally:
        db.close()


def test_pairs_within_tolerance_band(client, auth):
    """Notionals 1000 USDT and 1100 USDT (10% off) — within ±12% → pair."""
    from backend.db.base import SessionLocal
    from backend.services.trigger_order_service import auto_pair_internal_legs

    user = _user(client, auth)
    db = SessionLocal()
    try:
        _mk_position(db, user_id=user.id, leg_a_exchange="gate", leg_a_side="buy",
                      leg_a_qty=1000, leg_a_entry_price=1.0)        # 1000 USDT
        _mk_position(db, user_id=user.id, leg_a_exchange="mexc", leg_a_side="sell",
                      leg_a_qty=1100, leg_a_entry_price=1.0)        # 1100 USDT
        created = auto_pair_internal_legs(db, user.id)
        assert len(created) == 1
    finally:
        db.close()
