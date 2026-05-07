"""Task #3 — reconcile_service detects externally-closed arb positions.

Locks the closed-externally → status='closed' + cascade-cancel TP/SL
behaviour and the partial-close detection.
"""
import asyncio
from datetime import datetime


def _create_user(client, auth):
    """Just return alice — the auth fixture already registered her."""
    from backend.db.base import SessionLocal
    from backend.db.models import User
    db = SessionLocal()
    try:
        return db.query(User).filter(User.username == "alice").first()
    finally:
        db.close()


def test_arb_position_closed_externally_finalized(client, auth):
    """Both legs disappear from venue → arb_position.status='closed',
    closed_externally=True, TP/SL siblings cancelled."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition, ArbTriggerOrder
    from backend.services.reconcile_service import _reconcile_arb_positions

    user = _create_user(client, auth)
    db = SessionLocal()
    try:
        ap = ArbPosition(
            user_id=user.id, kind="long_short",
            long_exchange="gate", long_symbol="VANRY", long_wallet_id=1,
            short_exchange="mexc", short_symbol="VANRY", short_wallet_id=2,
            long_qty=1000, short_qty=1000,
            long_entry_price=0.005, short_entry_price=0.0051,
            entry_spread_pct=2.0,
            status="open",
        )
        db.add(ap)
        db.flush()

        tp = ArbTriggerOrder(
            user_id=user.id, arb_position_id=ap.id,
            kind="tp", trigger_spread_pct=0.3, status="pending",
        )
        db.add(tp)
        db.commit()
        ap_id = ap.id
        tp_id = tp.id
    finally:
        db.close()

    # Empty live_by_fp = both legs gone from venue
    db = SessionLocal()
    try:
        asyncio.run(_reconcile_arb_positions(db, user.id, {}))
    finally:
        db.close()

    db = SessionLocal()
    try:
        ap = db.query(ArbPosition).filter(ArbPosition.id == ap_id).first()
        tp = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == tp_id).first()
        assert ap.status == "closed"
        assert ap.closed_externally is True
        assert ap.closed_at is not None
        assert tp.status == "cancelled"
        assert tp.error_kind == "position_closed_externally"
    finally:
        db.close()


def test_arb_position_partial_when_only_one_leg_closes(client, auth):
    """One leg gone, other alive → status='partial', TP/SL stay active."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition, ArbTriggerOrder
    from backend.services.reconcile_service import _reconcile_arb_positions

    user = _create_user(client, auth)
    db = SessionLocal()
    try:
        ap = ArbPosition(
            user_id=user.id, kind="long_short",
            long_exchange="gate", long_symbol="VANRY", long_wallet_id=10,
            short_exchange="mexc", short_symbol="VANRY", short_wallet_id=20,
            long_qty=1000, short_qty=1000,
            status="open",
        )
        db.add(ap)
        db.flush()
        sl = ArbTriggerOrder(
            user_id=user.id, arb_position_id=ap.id,
            kind="sl", trigger_spread_pct=2.5, status="pending",
        )
        db.add(sl)
        db.commit()
        ap_id, sl_id = ap.id, sl.id
    finally:
        db.close()

    # Long leg still alive (wallet_id=10, VANRY, buy); short leg empty.
    live_by_fp = {
        (10, "VANRY", "buy"): {"quantity": 1000, "entry_price": 0.005},
    }
    db = SessionLocal()
    try:
        asyncio.run(_reconcile_arb_positions(db, user.id, live_by_fp))
    finally:
        db.close()

    db = SessionLocal()
    try:
        ap = db.query(ArbPosition).filter(ArbPosition.id == ap_id).first()
        sl = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == sl_id).first()
        assert ap.status == "partial"
        assert ap.long_qty == 1000
        assert ap.short_qty == 0
        assert sl.status == "pending"     # SL stays active on partial
    finally:
        db.close()


def test_arb_position_qty_synced_when_both_alive(client, auth):
    """Both legs still on venue but qty changed (e.g. user partial-closed
    one leg externally) → reconcile syncs qty, status stays 'open'."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition
    from backend.services.reconcile_service import _reconcile_arb_positions

    user = _create_user(client, auth)
    db = SessionLocal()
    try:
        ap = ArbPosition(
            user_id=user.id, kind="long_short",
            long_exchange="gate", long_symbol="LAB", long_wallet_id=100,
            short_exchange="aster", short_symbol="LAB", short_wallet_id=200,
            long_qty=500, short_qty=500,
            status="open",
        )
        db.add(ap)
        db.commit()
        ap_id = ap.id
    finally:
        db.close()

    # Long leg reduced from 500 → 300 externally, short still 500
    live_by_fp = {
        (100, "LAB", "buy"):  {"quantity": 300},
        (200, "LAB", "sell"): {"quantity": 500},
    }
    db = SessionLocal()
    try:
        asyncio.run(_reconcile_arb_positions(db, user.id, live_by_fp))
    finally:
        db.close()

    db = SessionLocal()
    try:
        ap = db.query(ArbPosition).filter(ArbPosition.id == ap_id).first()
        assert ap.status == "open"   # both alive
        assert ap.long_qty == 300    # reduced
        assert ap.short_qty == 500
    finally:
        db.close()
