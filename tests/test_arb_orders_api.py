"""Task 7 — /api/trade/arb-orders + /api/trade/arb-positions API tests.

Locks request shapes, validation rules, and the immediate-execution
warning behaviour.
"""
import pytest


def _create_perpdex_wallet(client, auth, dex):
    body = {
        "name": f"{dex} wallet",
        "wallet_type": "perpdex",
        "type_value": dex,
        "address": "0x1234567890abcdef1234567890abcdef12345678",
    }
    if dex == "paradex":
        body["api_token"] = "dummy"
    if dex == "aster":
        body["api_key"] = "k" * 16
        body["api_secret"] = "s" * 16
    r = client.post("/api/wallets", json=body, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _create_exchange_wallet(client, auth, ex):
    r = client.post("/api/wallets", json={
        "name": f"{ex} wallet",
        "wallet_type": "exchange",
        "type_value": ex,
        "api_key": "k" * 16,
        "api_secret": "s" * 16,
        "purpose": "screener",
    }, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _open_payload(long_id: int, short_id: int, **overrides):
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
        "leverage": 3,
        "margin_mode": "isolated",
    }
    body.update(overrides)
    return body


def test_create_open_trigger_minimal(client, auth):
    long = _create_exchange_wallet(client, auth, "gate")
    short = _create_exchange_wallet(client, auth, "mexc")
    r = client.post("/api/trade/arb-orders",
                    json=_open_payload(long["id"], short["id"]),
                    headers=auth)
    assert r.status_code == 200, r.text
    data = r.json()
    # No book.json in test env so no immediate-execution check fires
    assert "id" in data
    assert data["status"] in ("pending", "scheduled")


def test_create_open_with_tp_and_sl(client, auth):
    long = _create_exchange_wallet(client, auth, "gate")
    short = _create_exchange_wallet(client, auth, "mexc")
    r = client.post("/api/trade/arb-orders", json=_open_payload(
        long["id"], short["id"],
        tp={"trigger_spread_pct": 0.3},
        sl={"trigger_spread_pct": 2.5},
    ), headers=auth)
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["children"]) == 2


def test_validation_infinite_fill_requires_portion(client, auth):
    long = _create_exchange_wallet(client, auth, "gate")
    short = _create_exchange_wallet(client, auth, "mexc")
    r = client.post("/api/trade/arb-orders", json=_open_payload(
        long["id"], short["id"],
        infinite_fill=True,
    ), headers=auth)
    assert r.status_code == 422


def test_validation_portion_size_capped(client, auth):
    long = _create_exchange_wallet(client, auth, "gate")
    short = _create_exchange_wallet(client, auth, "mexc")
    r = client.post("/api/trade/arb-orders", json=_open_payload(
        long["id"], short["id"],
        portion_size_token=2000,   # > total_qty_token
    ), headers=auth)
    assert r.status_code == 422


def test_validation_close_requires_arb_position_id(client, auth):
    long = _create_exchange_wallet(client, auth, "gate")
    short = _create_exchange_wallet(client, auth, "mexc")
    r = client.post("/api/trade/arb-orders", json=_open_payload(
        long["id"], short["id"], kind="close",
    ), headers=auth)
    assert r.status_code == 422


def test_list_active_orders_filters_finalized(client, auth):
    long = _create_exchange_wallet(client, auth, "gate")
    short = _create_exchange_wallet(client, auth, "mexc")
    client.post("/api/trade/arb-orders",
                json=_open_payload(long["id"], short["id"]),
                headers=auth)

    r = client.get("/api/trade/arb-orders", headers=auth)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["status"] in ("pending", "scheduled")
    assert rows[0]["kind"] == "open"
    assert rows[0]["portions_filled"] == 0


def test_cancel_order_marks_cancelled(client, auth):
    long = _create_exchange_wallet(client, auth, "gate")
    short = _create_exchange_wallet(client, auth, "mexc")
    r = client.post("/api/trade/arb-orders",
                    json=_open_payload(long["id"], short["id"]),
                    headers=auth)
    oid = r.json()["id"]

    r = client.delete(f"/api/trade/arb-orders/{oid}", headers=auth)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    # Should now appear in /history not /arb-orders
    r = client.get("/api/trade/arb-orders", headers=auth)
    assert all(row["id"] != oid for row in r.json())
    r = client.get("/api/trade/arb-orders/history", headers=auth)
    assert any(row["id"] == oid for row in r.json())


def test_patch_locked_after_firing(client, auth):
    """PATCH on a row with status='firing' rejects with 409."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbTriggerOrder

    long = _create_exchange_wallet(client, auth, "gate")
    short = _create_exchange_wallet(client, auth, "mexc")
    r = client.post("/api/trade/arb-orders",
                    json=_open_payload(long["id"], short["id"]),
                    headers=auth)
    oid = r.json()["id"]

    # Manually flip to firing
    db = SessionLocal()
    try:
        o = db.query(ArbTriggerOrder).filter(ArbTriggerOrder.id == oid).first()
        o.status = "firing"
        db.commit()
    finally:
        db.close()

    r = client.patch(f"/api/trade/arb-orders/{oid}",
                     json={"trigger_spread_pct": 2.0}, headers=auth)
    assert r.status_code == 409


def test_other_user_cant_see_or_cancel(client, auth):
    """Trigger created by alice; bob tries to access — 404."""
    from fastapi.testclient import TestClient
    long = _create_exchange_wallet(client, auth, "gate")
    short = _create_exchange_wallet(client, auth, "mexc")
    r = client.post("/api/trade/arb-orders",
                    json=_open_payload(long["id"], short["id"]),
                    headers=auth)
    oid = r.json()["id"]

    # Register bob as a separate user
    rb = client.post("/api/auth/register", json={
        "username": "bob", "email": "bob@x.com", "password": "password123",
    })
    assert rb.status_code in (200, 201), rb.text
    bob_token = rb.json()["access_token"]
    bob_auth = {"Authorization": f"Bearer {bob_token}"}

    # Bob's GET — empty list
    r = client.get("/api/trade/arb-orders", headers=bob_auth)
    assert r.status_code == 200
    assert r.json() == []

    # Bob's DELETE — 404 (not seen)
    r = client.delete(f"/api/trade/arb-orders/{oid}", headers=bob_auth)
    assert r.status_code == 404


def test_arb_positions_list_empty(client, auth):
    r = client.get("/api/trade/arb-positions", headers=auth)
    assert r.status_code == 200
    assert r.json() == []


def test_attach_tp_to_open_position(client, auth):
    """Manually create an arb_position and attach a TP via PATCH."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition, User

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == "alice").first()
        long = _create_exchange_wallet(client, auth, "gate")
        short = _create_exchange_wallet(client, auth, "mexc")
        pos = ArbPosition(
            user_id=u.id, kind="long_short",
            long_exchange="gate", long_symbol="VANRY", long_wallet_id=long["id"],
            short_exchange="mexc", short_symbol="VANRY", short_wallet_id=short["id"],
            long_qty=1000, short_qty=1000,
            long_entry_price=0.005, short_entry_price=0.0051,
            entry_spread_pct=2.0,
            status="open",
        )
        db.add(pos)
        db.commit()
        pid = pos.id
    finally:
        db.close()

    r = client.patch(f"/api/trade/arb-positions/{pid}",
                     json={"tp": {"trigger_spread_pct": 0.3}},
                     headers=auth)
    assert r.status_code == 200, r.text
    assert len(r.json()["trigger_ids"]) == 1


def test_attach_duplicate_tp_returns_409(client, auth):
    from backend.db.base import SessionLocal
    from backend.db.models import ArbPosition, ArbTriggerOrder, User

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == "alice").first()
        long = _create_exchange_wallet(client, auth, "gate")
        short = _create_exchange_wallet(client, auth, "mexc")
        pos = ArbPosition(
            user_id=u.id, kind="long_short",
            long_exchange="gate", long_symbol="VANRY", long_wallet_id=long["id"],
            short_exchange="mexc", short_symbol="VANRY", short_wallet_id=short["id"],
            status="open", long_qty=1000, short_qty=1000,
        )
        db.add(pos)
        db.flush()
        existing = ArbTriggerOrder(
            user_id=u.id, arb_position_id=pos.id, kind="tp",
            trigger_spread_pct=0.3, status="pending",
        )
        db.add(existing)
        db.commit()
        pid = pos.id
    finally:
        db.close()

    r = client.patch(f"/api/trade/arb-positions/{pid}",
                     json={"tp": {"trigger_spread_pct": 0.5}},
                     headers=auth)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "tp_already_exists"


def test_immediate_execution_warning_with_force_bypass(client, auth, monkeypatch):
    """When current spread already meets trigger, return 200 + warning.
    Then re-POST with force=true to actually create."""
    from backend.services import trigger_order_service

    # Stub _load_books_json to return a tight spread
    fake_books = {
        "gate": {"VANRY": {"asks": [[100.0, 1000]], "bids": [[99, 1000]],
                            "ts": __import__("time").time()}},
        "mexc": {"VANRY": {"asks": [[103, 1000]], "bids": [[102, 1000]],
                            "ts": __import__("time").time()}},
    }
    monkeypatch.setattr(trigger_order_service, "_load_books_json", lambda: fake_books)
    # Patch the import inside arb_orders too
    from backend.api.v1 import arb_orders as ao
    monkeypatch.setattr(ao, "_load_books_json", lambda: fake_books)

    long = _create_exchange_wallet(client, auth, "gate")
    short = _create_exchange_wallet(client, auth, "mexc")

    # Trigger 1.5%; current effective spread is ~2.0% (already met)
    payload = _open_payload(long["id"], short["id"], trigger_spread_pct=1.5)
    r = client.post("/api/trade/arb-orders", json=payload, headers=auth)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("warning") == "immediate_execution"
    assert data["kind"] == "open"
    assert "id" not in data  # no order was created

    # Re-post with force=true → order created
    payload["force"] = True
    r = client.post("/api/trade/arb-orders", json=payload, headers=auth)
    assert r.status_code == 200
    assert "id" in r.json()
    assert r.json().get("warning") is None
