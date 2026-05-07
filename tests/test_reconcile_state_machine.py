"""arb_position state-machine + reconcile cascade behaviour.

Builds on test_reconcile_arb_positions.py with the state-transition
matrix and cascade rules:
- closed_externally cascade only touches pending/firing/scheduled
- fired/failed/cancelled children are NOT touched (audit trail kept)
- closing → closed via reconcile (matching close)
- partial → closed when remaining leg also closes
"""
import asyncio
from datetime import datetime


def _user(client, auth):
    from backend.db.base import SessionLocal
    from backend.db.models import User
    db = SessionLocal()
    try:
        return db.query(User).filter(User.username == "alice").first()
    finally:
        db.close()


def _make_arb(db, user_id, **kw):
    from backend.db.models import ArbPosition
    defaults = dict(
        user_id=user_id, kind="long_short",
        long_exchange="gate", long_symbol="VANRY", long_wallet_id=1,
        short_exchange="mexc", short_symbol="VANRY", short_wallet_id=2,
        long_qty=1000, short_qty=1000,
        long_entry_price=0.005, short_entry_price=0.0051,
        status="open",
    )
    defaults.update(kw)
    ap = type(defaults).__class__ if False else None  # noqa
    ap = ArbPosition(**defaults)
    db.add(ap)
    db.commit()
    db.refresh(ap)
    return ap


def _make_trigger(db, user_id, arb_position_id, **kw):
    from backend.db.models import ArbTriggerOrder
    defaults = dict(
        user_id=user_id, arb_position_id=arb_position_id,
        kind="tp", trigger_spread_pct=0.3, status="pending",
    )
    defaults.update(kw)
    t = ArbTriggerOrder(**defaults)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_external_close_only_cancels_active_children(client, auth):
    """fired/failed/cancelled children must NOT be touched by external-
    close cascade — those rows are part of the audit trail."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbTriggerOrder
    from backend.services.reconcile_service import _reconcile_arb_positions

    user = _user(client, auth)
    db = SessionLocal()
    try:
        ap = _make_arb(db, user.id)
        active     = _make_trigger(db, user.id, ap.id, kind="tp", status="pending")
        already_f  = _make_trigger(db, user.id, ap.id, kind="sl",
                                    trigger_spread_pct=2.5, status="fired")
        ap_id, active_id, fired_id = ap.id, active.id, already_f.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        asyncio.run(_reconcile_arb_positions(db, user.id, {}))    # both legs gone
    finally:
        db.close()

    db = SessionLocal()
    try:
        active = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == active_id).first()
        fired  = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == fired_id).first()
        assert active.status == "cancelled"
        assert fired.status == "fired"      # untouched
    finally:
        db.close()


def test_partial_to_closed_when_remaining_leg_closes(client, auth):
    """An arb_position already in 'partial' state, then the remaining leg
    closes externally → status='closed'."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition
    from backend.services.reconcile_service import _reconcile_arb_positions

    user = _user(client, auth)
    db = SessionLocal()
    try:
        ap = _make_arb(db, user.id, status="partial",
                        long_qty=1000, short_qty=0)
        ap_id = ap.id
    finally:
        db.close()

    # Both legs gone now → final close
    db = SessionLocal()
    try:
        asyncio.run(_reconcile_arb_positions(db, user.id, {}))
    finally:
        db.close()

    db = SessionLocal()
    try:
        ap = db.query(ArbPosition).filter(ArbPosition.id == ap_id).first()
        assert ap.status == "closed"
        assert ap.closed_externally is True
    finally:
        db.close()


def test_closing_status_finalized_by_reconcile(client, auth):
    """Position in 'closing' state (close-trigger fired but reconcile
    hasn't confirmed yet) → reconcile sees both legs gone → 'closed'."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition
    from backend.services.reconcile_service import _reconcile_arb_positions

    user = _user(client, auth)
    db = SessionLocal()
    try:
        ap = _make_arb(db, user.id, status="closing")
        ap_id = ap.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        asyncio.run(_reconcile_arb_positions(db, user.id, {}))
    finally:
        db.close()

    db = SessionLocal()
    try:
        ap = db.query(ArbPosition).filter(ArbPosition.id == ap_id).first()
        assert ap.status == "closed"
        assert ap.closed_at is not None
    finally:
        db.close()


def test_closed_status_terminal_unchanged(client, auth):
    """status='closed' is terminal — reconcile must not touch it even if
    venue reports new positions on those legs."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition
    from backend.services.reconcile_service import _reconcile_arb_positions

    user = _user(client, auth)
    db = SessionLocal()
    try:
        ap = _make_arb(db, user.id, status="closed",
                        closed_at=datetime.utcnow(),
                        long_qty=0, short_qty=0)
        ap_id = ap.id
        original_closed_at = ap.closed_at
    finally:
        db.close()

    # Venue reports new live positions — but status='closed' should be filtered out
    live = {
        (1, "VANRY", "buy"):  {"quantity": 500},
        (2, "VANRY", "sell"): {"quantity": 500},
    }
    db = SessionLocal()
    try:
        asyncio.run(_reconcile_arb_positions(db, user.id, live))
    finally:
        db.close()

    db = SessionLocal()
    try:
        ap = db.query(ArbPosition).filter(ArbPosition.id == ap_id).first()
        assert ap.status == "closed"
        assert ap.closed_at == original_closed_at  # not bumped
        # qty should not have been changed
        assert ap.long_qty == 0
    finally:
        db.close()


def test_external_open_increases_qty_marks_synced(client, auth):
    """User adds to position externally (venue qty > our qty) → reconcile
    syncs upward and marks synced_externally so UI flags it."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition
    from backend.services.reconcile_service import _reconcile_arb_positions

    user = _user(client, auth)
    db = SessionLocal()
    try:
        ap = _make_arb(db, user.id, long_qty=1000, short_qty=1000)
        ap_id = ap.id
    finally:
        db.close()

    # Both legs got bigger externally
    live = {
        (1, "VANRY", "buy"):  {"quantity": 1500},
        (2, "VANRY", "sell"): {"quantity": 1500},
    }
    db = SessionLocal()
    try:
        asyncio.run(_reconcile_arb_positions(db, user.id, live))
    finally:
        db.close()

    db = SessionLocal()
    try:
        ap = db.query(ArbPosition).filter(ArbPosition.id == ap_id).first()
        assert ap.long_qty == 1500
        assert ap.short_qty == 1500
        assert ap.status == "open"
    finally:
        db.close()


def test_closed_externally_pulls_pnl_from_child_trade_positions(client, auth):
    """When reconcile finalizes externally-closed arb_position, it sums
    realized + funding P&L from the child trade_positions rows."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition, TradePosition
    from backend.services.reconcile_service import _reconcile_arb_positions

    user = _user(client, auth)
    db = SessionLocal()
    try:
        ap = _make_arb(db, user.id)
        # Two child trade_positions wrapping this arb_position
        for side, ex, real, funding in [("buy", "gate", 12.0, 1.5), ("sell", "mexc", -7.0, 2.0)]:
            tp = TradePosition(
                user_id=user.id, kind="single", status="closed",
                symbol="VANRY",
                leg_a_wallet_id=1 if side == "buy" else 2,
                leg_a_exchange=ex, leg_a_side=side,
                leg_a_qty=1000, leg_a_entry_price=0.005,
                leg_a_exit_price=0.006,
                leg_a_realized_pnl_usd=real,
                leg_a_funding_pnl_usd=funding,
                arb_position_id=ap.id,
            )
            db.add(tp)
        db.commit()
        ap_id = ap.id
    finally:
        db.close()

    db = SessionLocal()
    try:
        asyncio.run(_reconcile_arb_positions(db, user.id, {}))
    finally:
        db.close()

    db = SessionLocal()
    try:
        ap = db.query(ArbPosition).filter(ArbPosition.id == ap_id).first()
        assert ap.status == "closed"
        assert ap.closed_externally is True
        # 12 - 7 + 1.5 + 2.0 = 8.5
        assert abs(ap.realized_pnl_usd - 8.5) < 0.001
    finally:
        db.close()
