"""Plan-based limits on active trigger orders.

Default caps (in `_enforce_trigger_limit` in arb_orders.py):
  Free  (trade_delay_ms > 0): 3
  Paid  (trade_delay_ms == 0): 50
  Plans can override via plans.features['max_active_triggers']; -1 means
  unlimited.

A 4th create on Free → HTTP 402 with `{error:"trigger_limit_exceeded"}`.
"""


def _make_order_payload(long_id, short_id, **overrides):
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
    body.update(overrides)
    return body


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


def test_free_plan_caps_at_three_active_triggers(client, auth):
    """Free user (trade_delay_ms=500) can create 3, then 4th returns 402."""
    long_id, short_id = _create_wallets(client, auth)
    for i in range(3):
        r = client.post("/api/trade/arb-orders",
                        json=_make_order_payload(long_id, short_id), headers=auth)
        assert r.status_code == 200, f"trigger #{i+1}: {r.text}"
    r = client.post("/api/trade/arb-orders",
                    json=_make_order_payload(long_id, short_id), headers=auth)
    assert r.status_code == 402
    body = r.json()
    detail = body.get("detail") or {}
    assert detail.get("error") == "trigger_limit_exceeded"
    assert detail.get("limit") == 3
    assert detail.get("current") == 3


def test_cancelled_does_not_count_toward_limit(client, auth):
    """A trigger that's been cancelled frees up a slot."""
    long_id, short_id = _create_wallets(client, auth)
    ids = []
    for _ in range(3):
        r = client.post("/api/trade/arb-orders",
                        json=_make_order_payload(long_id, short_id), headers=auth)
        ids.append(r.json()["id"])

    # 4th would 402 — cancel one first, then succeed
    client.delete(f"/api/trade/arb-orders/{ids[0]}", headers=auth)

    r = client.post("/api/trade/arb-orders",
                    json=_make_order_payload(long_id, short_id), headers=auth)
    assert r.status_code == 200, r.text


def test_paid_plan_lifts_limit_to_50(client, admin_auth):
    """Admin promotes themselves to a non-free plan (trade_delay_ms=0) and
    can create well past 3 triggers."""
    from backend.db.base import SessionLocal
    from backend.db.models import User, Plan

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == "admin").first()
        full = db.query(Plan).filter(Plan.slug == "full").first()
        u.plan_id = full.id
        db.commit()
    finally:
        db.close()

    long_id, short_id = _create_wallets(client, admin_auth)
    # Create 4 — was the cap on free
    for i in range(4):
        r = client.post("/api/trade/arb-orders",
                        json=_make_order_payload(long_id, short_id), headers=admin_auth)
        assert r.status_code == 200, f"trigger #{i+1}: {r.text}"
