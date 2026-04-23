"""CSV / JSON export of current balances."""
from __future__ import annotations

from backend.db.models import BalanceSnapshot


def _register(client, username, email, password="password123"):
    r = client.post("/api/auth/register", json={
        "username": username, "email": email, "password": password,
    })
    assert r.status_code in (200, 201)
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_export_requires_auth(client):
    r = client.get("/api/portfolio/export")
    assert r.status_code == 401


def test_export_empty_returns_header_only_csv(client):
    t = _register(client, "alice", "alice@test.com")
    r = client.get("/api/portfolio/export", headers=_auth(t))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    body = r.text.strip().split("\n")
    assert body[0].startswith("wallet_id,wallet_name,wallet_type,type_value,asset,amount,snapshot_at,stable_total_usd")
    assert len(body) == 1  # header only


def test_export_json_format(client):
    t = _register(client, "alice", "alice@test.com")
    r = client.get("/api/portfolio/export?format=json", headers=_auth(t))
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_export_csv_with_snapshot(client):
    """Seed a BalanceSnapshot row and confirm the CSV picks it up."""
    from unittest.mock import AsyncMock
    import pytest  # noqa: F401 — fixture used below

    t = _register(client, "alice", "alice@test.com")

    # Create a chain wallet (no adapter validate_key → no network call)
    r_w = client.post("/api/wallets", json={
        "name": "eth main", "wallet_type": "chain",
        "type_value": "ethereum", "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    }, headers=_auth(t))
    assert r_w.status_code == 201
    wallet = r_w.json()

    # Seed a BalanceSnapshot directly in the test DB
    from tests.conftest import _Session
    session = _Session()
    try:
        session.add(BalanceSnapshot(
            wallet_id=wallet["id"], user_id=1,
            totals={"ETH": "1.2345", "USDT": "500.00"},
            stable_total=500.0,
        ))
        session.commit()
    finally:
        session.close()

    r = client.get("/api/portfolio/export", headers=_auth(t))
    assert r.status_code == 200
    lines = r.text.strip().split("\n")
    # Header + two asset rows for the one wallet
    assert len(lines) == 3
    body = "\n".join(lines[1:])
    assert "ETH" in body and "USDT" in body
    assert "0xd8dA" not in body  # addresses stay in credentials, not export
