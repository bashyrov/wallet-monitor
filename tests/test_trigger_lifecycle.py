"""End-to-end lifecycle tests for the trigger-order state machine.

Covers transitions that the unit tests in test_trigger_orders.py touch
only at the function-level — these go through the full service code
path with mocked trade_service so we exercise the actual branching.

State machine (per DEV_PROMPT.md §7.6.E):
  scheduled → pending → firing → fired | failed | cancelled
                              \\
                               → pending (if more portions / infinite_fill)
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest


def _user(client, auth):
    from backend.db.base import SessionLocal
    from backend.db.models import User
    db = SessionLocal()
    try:
        return db.query(User).filter(User.username == "alice").first()
    finally:
        db.close()


def _make_trigger(db, user_id, **kw):
    from backend.db.models import ArbTriggerOrder
    defaults = dict(
        user_id=user_id, kind="open", trigger_spread_pct=1.5,
        long_exchange="gate", long_symbol="VANRY", long_wallet_id=1,
        short_exchange="mexc", short_symbol="VANRY", short_wallet_id=2,
        total_qty_token=100.0, status="pending",
    )
    defaults.update(kw)
    t = ArbTriggerOrder(**defaults)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ── Scheduled → pending promotion ──────────────────────────────────────────
def test_scheduled_promoted_when_activate_at_reached(client, auth):
    """Triggers with activate_at in the past flip to 'pending' on next tick."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbTriggerOrder
    from backend.services.trigger_order_service import _tick

    user = _user(client, auth)
    past = datetime.utcnow() - timedelta(seconds=5)
    db = SessionLocal()
    try:
        _make_trigger(db, user.id, status="scheduled", activate_at=past)
        oid_id = db.query(ArbTriggerOrder).first().id
    finally:
        db.close()

    # Run a tick — books_json doesn't exist so condition won't be met,
    # but the scheduled-promotion happens regardless.
    db = SessionLocal()
    try:
        asyncio.run(_tick(db, books=None))
    finally:
        db.close()

    db = SessionLocal()
    try:
        t = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == oid_id).first()
        assert t.status == "pending"
    finally:
        db.close()


def test_scheduled_stays_when_activate_at_in_future(client, auth):
    """Future activate_at → row stays scheduled."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbTriggerOrder
    from backend.services.trigger_order_service import _tick

    user = _user(client, auth)
    future = datetime.utcnow() + timedelta(hours=1)
    db = SessionLocal()
    try:
        _make_trigger(db, user.id, status="scheduled", activate_at=future)
        oid = db.query(ArbTriggerOrder).first().id
    finally:
        db.close()

    db = SessionLocal()
    try:
        asyncio.run(_tick(db, books=None))
    finally:
        db.close()

    db = SessionLocal()
    try:
        t = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == oid).first()
        assert t.status == "scheduled"
    finally:
        db.close()


# ── Portion-fill loop ──────────────────────────────────────────────────────
def test_portion_loop_re_arms_until_target_reached(client, auth):
    """A trigger with portions_target=3 fires three times, then status=fired.
    Mocks trade_service.place_open_order to return clean fills."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbTriggerOrder, Wallet
    from backend.services import trigger_order_service as tos

    user = _user(client, auth)
    db = SessionLocal()
    try:
        # Real wallets needed for _execute_open_portion's wallet lookup
        for wid, ex in [(1, "gate"), (2, "mexc")]:
            db.add(Wallet(
                id=wid, user_id=user.id, name=f"{ex} wallet",
                wallet_type="exchange", type_value=ex,
                credentials={}, purpose="screener", can_trade=True,
            ))
        db.commit()
        t = _make_trigger(
            db, user.id,
            total_qty_token=300.0, portion_size_token=100.0, portions_target=3,
            status="firing",  # already claimed
        )
        oid = t.id
    finally:
        db.close()

    fake_fill = {"avg_fill_price": 0.005, "filled_qty": 100.0}
    import backend.services.trade_service as ts_mod
    with patch.object(ts_mod, 'place_open_order', AsyncMock(return_value=fake_fill)):
        for _ in range(3):
            db = SessionLocal()
            try:
                t = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == oid).first()
                if t.status == "pending":
                    t.status = "firing"
                    db.commit()
                asyncio.run(tos._execute_portion(db, t, snapshot_spread=2.0))
            finally:
                db.close()

    db = SessionLocal()
    try:
        t = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == oid).first()
        assert t.portions_filled == 3, f"expected 3 fills, got {t.portions_filled}"
        assert t.status == "fired"
    finally:
        db.close()


def test_infinite_fill_keeps_re_arming(client, auth):
    """infinite_fill=true — status flips to pending after each portion
    indefinitely until user cancels."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbTriggerOrder, Wallet
    from backend.services import trigger_order_service as tos
    import backend.services.trade_service as ts_mod

    user = _user(client, auth)
    db = SessionLocal()
    try:
        for wid, ex in [(11, "gate"), (12, "mexc")]:
            db.add(Wallet(
                id=wid, user_id=user.id, name=f"{ex} w",
                wallet_type="exchange", type_value=ex,
                credentials={}, purpose="screener", can_trade=True,
            ))
        db.commit()
        t = _make_trigger(
            db, user.id, long_wallet_id=11, short_wallet_id=12,
            total_qty_token=1000.0, portion_size_token=100.0,
            portions_target=10, infinite_fill=True, status="firing",
        )
        oid = t.id
    finally:
        db.close()

    fake = {"avg_fill_price": 0.005, "filled_qty": 100.0}
    with patch.object(ts_mod, 'place_open_order', AsyncMock(return_value=fake)):
        # Run 5 fires — far less than target, but infinite_fill should
        # keep status='pending' regardless of portions_filled.
        for _ in range(5):
            db = SessionLocal()
            try:
                t = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == oid).first()
                t.status = "firing"
                db.commit()
                asyncio.run(tos._execute_portion(db, t, snapshot_spread=2.0))
            finally:
                db.close()

    db = SessionLocal()
    try:
        t = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == oid).first()
        assert t.portions_filled == 5
        assert t.status == "pending", "infinite_fill must keep re-arming"
    finally:
        db.close()


# ── Parent open → cascade-promote children ────────────────────────────────
def test_parent_open_fired_promotes_children(client, auth):
    """When the parent open trigger reaches 'fired', any scheduled TP/SL
    children get promoted to 'pending'."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbTriggerOrder, Wallet
    from backend.services import trigger_order_service as tos
    import backend.services.trade_service as ts_mod

    user = _user(client, auth)
    db = SessionLocal()
    try:
        for wid, ex in [(21, "gate"), (22, "mexc")]:
            db.add(Wallet(
                id=wid, user_id=user.id, name=f"{ex} w",
                wallet_type="exchange", type_value=ex,
                credentials={}, purpose="screener", can_trade=True,
            ))
        db.commit()
        # Parent: single-shot open (portions_target=1)
        parent = _make_trigger(
            db, user.id, long_wallet_id=21, short_wallet_id=22,
            total_qty_token=100.0, portions_target=1, status="firing",
        )
        # TP + SL children, scheduled
        tp = _make_trigger(
            db, user.id, kind="tp", trigger_spread_pct=0.3,
            long_wallet_id=21, short_wallet_id=22,
            parent_trigger_id=parent.id, status="scheduled",
        )
        sl = _make_trigger(
            db, user.id, kind="sl", trigger_spread_pct=2.5,
            long_wallet_id=21, short_wallet_id=22,
            parent_trigger_id=parent.id, status="scheduled",
        )
        pid, tp_id, sl_id = parent.id, tp.id, sl.id
    finally:
        db.close()

    fake = {"avg_fill_price": 0.005, "filled_qty": 100.0}
    with patch.object(ts_mod, 'place_open_order', AsyncMock(return_value=fake)):
        db = SessionLocal()
        try:
            t = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == pid).first()
            asyncio.run(tos._execute_portion(db, t, snapshot_spread=2.0))
        finally:
            db.close()

    db = SessionLocal()
    try:
        parent = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == pid).first()
        tp = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == tp_id).first()
        sl = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == sl_id).first()
        assert parent.status == "fired"
        assert tp.status == "pending", "TP child must be promoted on parent fire"
        assert sl.status == "pending", "SL child must be promoted on parent fire"
    finally:
        db.close()


def test_partial_fill_marks_position_partial(client, auth):
    """Long fills, short rejects → arb_position.status='partial', trigger
    failed with error_kind='partial'. Per spec we never auto-revert."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition, ArbTriggerOrder, Wallet
    from backend.services import trigger_order_service as tos
    import backend.services.trade_service as ts_mod

    user = _user(client, auth)
    db = SessionLocal()
    try:
        for wid, ex in [(31, "gate"), (32, "mexc")]:
            db.add(Wallet(
                id=wid, user_id=user.id, name=f"{ex} w",
                wallet_type="exchange", type_value=ex,
                credentials={}, purpose="screener", can_trade=True,
            ))
        db.commit()
        t = _make_trigger(
            db, user.id, long_wallet_id=31, short_wallet_id=32,
            total_qty_token=100.0, status="firing",
        )
        oid = t.id
    finally:
        db.close()

    # Long succeeds, short raises — partial fill scenario
    long_ok = AsyncMock(return_value={"avg_fill_price": 0.005, "filled_qty": 100.0})
    short_fail = AsyncMock(side_effect=Exception("rate limited"))

    async def _disp(db, user_id, wallet_id, sym, side, qty, lev, mm):
        if side == "buy":
            return await long_ok(db, user_id, wallet_id, sym, side, qty, lev, mm)
        return await short_fail(db, user_id, wallet_id, sym, side, qty, lev, mm)

    with patch.object(ts_mod, 'place_open_order', _disp):
        db = SessionLocal()
        try:
            t = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == oid).first()
            asyncio.run(tos._execute_portion(db, t, snapshot_spread=2.0))
        finally:
            db.close()

    db = SessionLocal()
    try:
        t = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == oid).first()
        assert t.status == "failed"
        assert t.error_kind == "partial"
        # arb_position should exist with partial status (long leg accumulated)
        ap = db.query(ArbPosition).filter(ArbPosition.user_id == user.id).first()
        assert ap is not None
        assert ap.status == "partial"
        assert ap.long_qty == 100.0
        assert ap.short_qty == 0.0
    finally:
        db.close()
