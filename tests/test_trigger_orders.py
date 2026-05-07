"""Task 7 — trigger_order_service core tests.

Covers:
- Atomic claim-on-fire (cross-replica race protection)
- VWAP computation from orderbook levels
- Effective spread for size-aware fills
- Condition evaluation per kind
- Position accumulation across portions
- Auto-pair detection on internal opens

Live execution paths (_execute_open_portion / _execute_close) require
real venue mocking — covered indirectly via the API tests.
"""
from datetime import datetime, timedelta

import pytest


def test_vwap_from_levels_sufficient_depth():
    from backend.services.trigger_order_service import _vwap_from_levels

    levels = [[100.0, 5.0], [101.0, 10.0], [102.0, 20.0]]
    # Need 12 units → take 5 @ 100 + 7 @ 101 = (500 + 707) / 12 = 100.583...
    px = _vwap_from_levels(levels, 12.0)
    assert px is not None
    assert abs(px - (500 + 707) / 12) < 1e-6


def test_vwap_from_levels_insufficient_depth():
    from backend.services.trigger_order_service import _vwap_from_levels
    levels = [[100.0, 1.0], [101.0, 1.0]]
    assert _vwap_from_levels(levels, 100.0) is None


def test_vwap_from_levels_empty():
    from backend.services.trigger_order_service import _vwap_from_levels
    assert _vwap_from_levels([], 1.0) is None
    assert _vwap_from_levels([[100, 1]], 0) is None


def test_compute_effective_spread_basic():
    """Long buys into asks at 100; short sells into bids at 102.
    Effective spread = (102 - 100) / 100 * 100 = 2%.
    """
    from backend.services.trigger_order_service import _compute_effective_spread
    import time

    now_s = time.time()
    books = {
        "gate": {"VANRY": {"asks": [[100.0, 50.0]], "bids": [[99.0, 50.0]], "ts": now_s}},
        "mexc": {"VANRY": {"asks": [[103.0, 50.0]], "bids": [[102.0, 50.0]], "ts": now_s}},
    }
    spread = _compute_effective_spread(books, "gate", "VANRY", "mexc", "VANRY", 10.0)
    assert spread is not None
    assert abs(spread - 2.0) < 1e-6


def test_compute_effective_spread_stale_book():
    """If either book's ts is older than BOOKS_STALE_MAX_S → None."""
    from backend.services.trigger_order_service import _compute_effective_spread
    import time

    now_s = time.time()
    books = {
        "gate": {"VANRY": {"asks": [[100, 50]], "bids": [[99, 50]], "ts": now_s - 10}},
        "mexc": {"VANRY": {"asks": [[103, 50]], "bids": [[102, 50]], "ts": now_s}},
    }
    assert _compute_effective_spread(books, "gate", "VANRY", "mexc", "VANRY", 10.0) is None


def test_condition_met_open_widens():
    from backend.services.trigger_order_service import condition_met
    from backend.db.models import ArbTriggerOrder
    o = ArbTriggerOrder(kind="open", trigger_spread_pct=1.5)
    assert condition_met(o, 1.50) is True   # equality
    assert condition_met(o, 1.51) is True   # widened past
    assert condition_met(o, 1.49) is False


def test_condition_met_tp_converges():
    from backend.services.trigger_order_service import condition_met
    from backend.db.models import ArbTriggerOrder
    o = ArbTriggerOrder(kind="tp", trigger_spread_pct=0.3)
    assert condition_met(o, 0.30) is True
    assert condition_met(o, 0.29) is True   # converged below
    assert condition_met(o, 0.31) is False


def test_condition_met_sl_widens():
    from backend.services.trigger_order_service import condition_met
    from backend.db.models import ArbTriggerOrder
    o = ArbTriggerOrder(kind="sl", trigger_spread_pct=2.5)
    assert condition_met(o, 2.50) is True
    assert condition_met(o, 2.51) is True
    assert condition_met(o, 2.49) is False


def test_condition_met_market_trigger():
    """trigger_spread_pct=None means 'fire next tick' — always met."""
    from backend.services.trigger_order_service import condition_met
    from backend.db.models import ArbTriggerOrder
    o = ArbTriggerOrder(kind="open", trigger_spread_pct=None)
    assert condition_met(o, 0.0) is True
    assert condition_met(o, 100.0) is True
    assert condition_met(o, -100.0) is True


def test_vwap_merge_first_fill():
    from backend.services.trigger_order_service import vwap_merge
    assert vwap_merge(None, 0, 100.0, 10.0) == 100.0
    assert vwap_merge(0.0, 0, 100.0, 10.0) == 100.0


def test_vwap_merge_subsequent_fills():
    from backend.services.trigger_order_service import vwap_merge
    # First portion: 10 @ 100 → entry 100
    # Second portion: 10 @ 110 → vwap = (1000 + 1100) / 20 = 105
    px = vwap_merge(100.0, 10.0, 110.0, 10.0)
    assert abs(px - 105.0) < 1e-6


def test_atomic_claim_for_fire_winner_only(client, auth):
    """Two concurrent claims on the same trigger — exactly one wins."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbTriggerOrder, User
    from backend.services.trigger_order_service import claim_for_fire

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "alice").first()
        order = ArbTriggerOrder(
            user_id=user.id,
            kind="open",
            trigger_spread_pct=1.5,
            long_exchange="gate", long_symbol="VANRY",
            short_exchange="mexc", short_symbol="VANRY",
            total_qty_token=100.0,
            status="pending",
        )
        db.add(order)
        db.commit()
        oid = order.id
    finally:
        db.close()

    # Two separate sessions try to claim the same order
    db1 = SessionLocal()
    db2 = SessionLocal()
    try:
        won_1 = claim_for_fire(db1, oid)
        won_2 = claim_for_fire(db2, oid)
        # Exactly one wins
        assert won_1 != won_2, "claim race must produce exactly one winner"
        assert won_1 or won_2
    finally:
        db1.close(); db2.close()

    # The order must be in 'firing' state now
    db = SessionLocal()
    try:
        o = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == oid).first()
        assert o.status == "firing"
    finally:
        db.close()


def test_accumulate_position_first_fill(client, auth):
    from backend.db.base import SessionLocal
    from backend.db.models import ArbTriggerOrder, User
    from backend.services.trigger_order_service import accumulate_position

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "alice").first()
        trigger = ArbTriggerOrder(
            user_id=user.id, kind="open",
            long_exchange="gate", long_symbol="VANRY",
            short_exchange="mexc", short_symbol="VANRY",
            total_qty_token=100.0, status="firing",
        )
        db.add(trigger)
        db.flush()

        pos = accumulate_position(
            db, trigger,
            long_fill_price=0.005, long_fill_qty=100.0,
            short_fill_price=0.0051, short_fill_qty=100.0,
        )
        assert pos.long_qty == 100.0
        assert pos.short_qty == 100.0
        assert pos.long_entry_price == 0.005
        assert pos.short_entry_price == 0.0051
        assert pos.entry_spread_pct is not None
        assert abs(pos.entry_spread_pct - 2.0) < 0.01    # (.0051-.005)/.005 * 100 ≈ 2%
    finally:
        db.close()


def test_accumulate_position_vwap_across_portions(client, auth):
    """Portion 1: 100 @ 0.005, Portion 2: 100 @ 0.0055
    → VWAP entry = (0.5 + 0.55) / 200 = 0.00525
    """
    from backend.db.base import SessionLocal
    from backend.db.models import ArbTriggerOrder, User
    from backend.services.trigger_order_service import accumulate_position

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "alice").first()
        trigger = ArbTriggerOrder(
            user_id=user.id, kind="open",
            long_exchange="gate", long_symbol="VANRY",
            short_exchange="mexc", short_symbol="VANRY",
            total_qty_token=200.0, portion_size_token=100.0,
            status="firing",
        )
        db.add(trigger)
        db.flush()

        accumulate_position(db, trigger, 0.005, 100, 0.0051, 100)
        accumulate_position(db, trigger, 0.0055, 100, 0.0056, 100)
        from backend.db.models import ArbPosition
        pos = db.query(ArbPosition).filter(ArbPosition.id == trigger.arb_position_id).first()

        assert abs(pos.long_qty - 200.0) < 1e-6
        assert abs(pos.long_entry_price - 0.00525) < 1e-6
        assert abs(pos.short_entry_price - 0.00535) < 1e-6
    finally:
        db.close()
