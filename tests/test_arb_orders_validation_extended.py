"""Extended /api/trade/arb-orders validation + cancellation behaviour.

Locks behaviours not covered by test_arb_orders_api.py:
- Cross-user wallet rejection (security)
- DELETE cascades to children
- PATCH locked once status flips out of pending/scheduled
- TP/SL spec validations
- portions_target computed from total ÷ portion correctly
- Edit lifecycle invariants
"""
from datetime import datetime


def _create_wallets(client, auth):
    L = client.post("/api/wallets", json={
        "name": "gate w", "wallet_type": "exchange", "type_value": "gate",
        "api_key": "k" * 16, "api_secret": "s" * 16, "purpose": "screener",
    }, headers=auth).json()
    S = client.post("/api/wallets", json={
        "name": "mexc w", "wallet_type": "exchange", "type_value": "mexc",
        "api_key": "k" * 16, "api_secret": "s" * 16, "purpose": "screener",
    }, headers=auth).json()
    return L["id"], S["id"]


def _open_payload(long_id, short_id, **kw):
    body = {
        "kind": "open",
        "pair_kind": "long_short",
        "long_exchange": "gate",
        "long_symbol": "VANRY",
        "long_wallet_id": long_id,
        "short_exchange": "mexc",
        "short_symbol": "VANRY",
        "short_wallet_id": short_id,
        "trigger_spread_pct": 1.5,
        "total_qty_token": 1000.0,
    }
    body.update(kw)
    return body


def test_rejects_other_users_wallet_id(client, auth):
    """User A cannot use User B's wallet_id in an arb order."""
    long_id, short_id = _create_wallets(client, auth)
    # Bob registers
    rb = client.post("/api/auth/register", json={
        "username": "bob", "email": "bob@x.com", "password": "password123",
    })
    bob_token = rb.json()["access_token"]
    bob_auth = {"Authorization": f"Bearer {bob_token}"}

    # Bob tries to use alice's wallet_id
    r = client.post("/api/trade/arb-orders",
                    json=_open_payload(long_id, short_id), headers=bob_auth)
    assert r.status_code == 404
    assert "not found" in (r.json().get("detail") or "").lower() or "owned" in (r.json().get("detail") or "")


def test_portions_target_computed_correctly(client, auth):
    """total=1000, portion=300 → portions_target=ceil(1000/300)=4"""
    long_id, short_id = _create_wallets(client, auth)
    r = client.post("/api/trade/arb-orders",
                    json=_open_payload(long_id, short_id,
                                        total_qty_token=1000.0,
                                        portion_size_token=300.0),
                    headers=auth)
    assert r.status_code == 200, r.text
    oid = r.json()["id"]

    rows = client.get("/api/trade/arb-orders", headers=auth).json()
    row = next(r for r in rows if r["id"] == oid)
    assert row["portions_target"] == 4


def test_portions_target_one_when_no_portion_size(client, auth):
    """No portion_size_token → portions_target=1 (single-shot)."""
    long_id, short_id = _create_wallets(client, auth)
    r = client.post("/api/trade/arb-orders",
                    json=_open_payload(long_id, short_id, total_qty_token=500.0),
                    headers=auth)
    oid = r.json()["id"]
    rows = client.get("/api/trade/arb-orders", headers=auth).json()
    row = next(r for r in rows if r["id"] == oid)
    assert row["portions_target"] == 1


def test_delete_cascades_to_children(client, auth):
    """Cancelling a parent open trigger also cancels its scheduled
    TP/SL children — DB rows go to status='cancelled'."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbTriggerOrder

    long_id, short_id = _create_wallets(client, auth)
    r = client.post("/api/trade/arb-orders", json=_open_payload(
        long_id, short_id,
        tp={"trigger_spread_pct": 0.3},
        sl={"trigger_spread_pct": 2.5},
    ), headers=auth)
    parent_id = r.json()["id"]
    child_ids = r.json()["children"]

    r = client.delete(f"/api/trade/arb-orders/{parent_id}", headers=auth)
    assert r.status_code == 200

    # Children should be cancelled too
    db = SessionLocal()
    try:
        for cid in child_ids:
            c = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == cid).first()
            assert c is not None, f"child {cid} disappeared"
            assert c.status == "cancelled", f"child {cid} should be cancelled, got {c.status}"
    finally:
        db.close()


def test_patch_locked_once_fired(client, auth):
    """PATCH on a row whose status flipped to 'fired' returns 409."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbTriggerOrder

    long_id, short_id = _create_wallets(client, auth)
    r = client.post("/api/trade/arb-orders",
                    json=_open_payload(long_id, short_id), headers=auth)
    oid = r.json()["id"]

    db = SessionLocal()
    try:
        t = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == oid).first()
        t.status = "fired"
        db.commit()
    finally:
        db.close()

    r = client.patch(f"/api/trade/arb-orders/{oid}",
                     json={"trigger_spread_pct": 2.0}, headers=auth)
    assert r.status_code == 409


def test_close_kind_requires_arb_position_id(client, auth):
    """kind='close' without arb_position_id → 422 (already covered, but
    re-asserts as part of full validation matrix)."""
    long_id, short_id = _create_wallets(client, auth)
    r = client.post("/api/trade/arb-orders",
                    json=_open_payload(long_id, short_id, kind="close"),
                    headers=auth)
    assert r.status_code == 422


def test_tp_sl_only_allowed_on_open_kind(client, auth):
    """tp/sl nested in a 'close' payload → 422."""
    long_id, short_id = _create_wallets(client, auth)
    r = client.post("/api/trade/arb-orders", json=_open_payload(
        long_id, short_id, kind="close", arb_position_id=1,
        tp={"trigger_spread_pct": 0.3},
    ), headers=auth)
    assert r.status_code == 422


def test_cancel_already_cancelled_returns_409(client, auth):
    """Cancelling twice → second call 409."""
    long_id, short_id = _create_wallets(client, auth)
    r = client.post("/api/trade/arb-orders",
                    json=_open_payload(long_id, short_id), headers=auth)
    oid = r.json()["id"]

    r1 = client.delete(f"/api/trade/arb-orders/{oid}", headers=auth)
    assert r1.status_code == 200
    r2 = client.delete(f"/api/trade/arb-orders/{oid}", headers=auth)
    assert r2.status_code == 409


def test_history_endpoint_returns_only_finalized(client, auth):
    """GET /history shows fired/failed/cancelled, NOT pending/firing."""
    long_id, short_id = _create_wallets(client, auth)
    r = client.post("/api/trade/arb-orders",
                    json=_open_payload(long_id, short_id), headers=auth)
    oid = r.json()["id"]

    # Active list shows it; history doesn't
    active = client.get("/api/trade/arb-orders", headers=auth).json()
    history = client.get("/api/trade/arb-orders/history", headers=auth).json()
    assert any(row["id"] == oid for row in active)
    assert all(row["id"] != oid for row in history)

    client.delete(f"/api/trade/arb-orders/{oid}", headers=auth)

    active2 = client.get("/api/trade/arb-orders", headers=auth).json()
    history2 = client.get("/api/trade/arb-orders/history", headers=auth).json()
    assert all(row["id"] != oid for row in active2)
    assert any(row["id"] == oid for row in history2)


def test_patch_updates_trigger_spread(client, auth):
    """PATCH allows changing trigger_spread_pct on a pending row."""
    long_id, short_id = _create_wallets(client, auth)
    r = client.post("/api/trade/arb-orders",
                    json=_open_payload(long_id, short_id,
                                        trigger_spread_pct=1.5), headers=auth)
    oid = r.json()["id"]

    r = client.patch(f"/api/trade/arb-orders/{oid}",
                     json={"trigger_spread_pct": 1.8}, headers=auth)
    assert r.status_code == 200, r.text

    rows = client.get("/api/trade/arb-orders", headers=auth).json()
    row = next(r for r in rows if r["id"] == oid)
    assert row["trigger_spread_pct"] == 1.8


def test_negative_trigger_spread_accepted(client, auth):
    """Negative spread (short side cheaper than long) is a legal arb opp.
    The schema is float, not constrained-positive."""
    long_id, short_id = _create_wallets(client, auth)
    r = client.post("/api/trade/arb-orders",
                    json=_open_payload(long_id, short_id, trigger_spread_pct=-0.5),
                    headers=auth)
    assert r.status_code == 200, r.text


def test_market_trigger_null_spread_accepted(client, auth):
    """trigger_spread_pct=null (placeholder Last) → market trigger,
    fires next tick. Skip the immediate-execution check."""
    long_id, short_id = _create_wallets(client, auth)
    r = client.post("/api/trade/arb-orders",
                    json=_open_payload(long_id, short_id, trigger_spread_pct=None),
                    headers=auth)
    assert r.status_code == 200
    assert "warning" not in r.json()


def test_attaching_tp_to_closed_position_rejected(client, auth):
    """PATCH /arb-positions/{id} requires status IN ('open','partial').
    Closed → 409."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition, User
    long_id, short_id = _create_wallets(client, auth)
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == "alice").first()
        ap = ArbPosition(
            user_id=u.id, kind="long_short",
            long_exchange="gate", long_symbol="VANRY", long_wallet_id=long_id,
            short_exchange="mexc", short_symbol="VANRY", short_wallet_id=short_id,
            status="closed", long_qty=0, short_qty=0,
        )
        db.add(ap)
        db.commit()
        pid = ap.id
    finally:
        db.close()

    r = client.patch(f"/api/trade/arb-positions/{pid}",
                     json={"tp": {"trigger_spread_pct": 0.3}}, headers=auth)
    assert r.status_code == 409


def test_attach_tp_sl_must_specify_at_least_one(client, auth):
    """PATCH /arb-positions/{id} with empty body → 422."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition, User
    long_id, short_id = _create_wallets(client, auth)
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == "alice").first()
        ap = ArbPosition(
            user_id=u.id, kind="long_short",
            long_exchange="gate", long_symbol="VANRY", long_wallet_id=long_id,
            short_exchange="mexc", short_symbol="VANRY", short_wallet_id=short_id,
            status="open", long_qty=1000, short_qty=1000,
        )
        db.add(ap)
        db.commit()
        pid = ap.id
    finally:
        db.close()

    r = client.patch(f"/api/trade/arb-positions/{pid}", json={}, headers=auth)
    assert r.status_code == 422


def test_arb_positions_list_isolation(client, auth):
    """User A's arb_positions are not visible to User B."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition, User
    long_id, short_id = _create_wallets(client, auth)
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == "alice").first()
        ap = ArbPosition(
            user_id=u.id, kind="long_short",
            long_exchange="gate", long_symbol="VANRY", long_wallet_id=long_id,
            short_exchange="mexc", short_symbol="VANRY", short_wallet_id=short_id,
            status="open", long_qty=1000, short_qty=1000,
        )
        db.add(ap)
        db.commit()
    finally:
        db.close()

    rb = client.post("/api/auth/register", json={
        "username": "bob", "email": "bob@x.com", "password": "password123",
    })
    bob_auth = {"Authorization": f"Bearer {rb.json()['access_token']}"}

    r = client.get("/api/trade/arb-positions", headers=bob_auth)
    assert r.status_code == 200
    assert r.json() == []


def test_sync_endpoint_idempotent(client, auth):
    """Calling /arb-positions/sync twice in a row is safe — auto_pair won't
    re-wrap already-wrapped legs."""
    long_id, short_id = _create_wallets(client, auth)
    # No legs to pair — sync just returns count=0 cleanly
    r1 = client.post("/api/trade/arb-positions/sync", headers=auth)
    r2 = client.post("/api/trade/arb-positions/sync", headers=auth)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["count"] == 0
    assert r2.json()["count"] == 0
