"""Wallet CRUD — all 8 exchanges, 13 chains, 4 perp DEXes, archive, tags."""
from unittest.mock import AsyncMock
import pytest


@pytest.fixture(autouse=True)
def _stub_exchange_validate_key(monkeypatch):
    """Wallet-create calls `adapter.validate_key(creds)` against the live
    exchange. That's network-dependent (Binance 451 on GH runners, MEXC 500s,
    etc) and not what we're testing here. Stub every known adapter's
    validate_key to report a clean key so the route runs through to
    create_wallet() and we can assert persistence / display masking.
    """
    from backend.services import trade_adapters
    ok = {"can_read": True, "can_trade": True, "error": None}
    for name, adapter in (trade_adapters.ADAPTERS or {}).items():
        if hasattr(adapter, "validate_key"):
            monkeypatch.setattr(adapter, "validate_key", AsyncMock(return_value=ok))
    yield


# ── Helpers ───────────────────────────────────────────────────────────────────

def _exchange(client, auth, exchange, name=None, passphrase=None):
    body = {
        "name": name or f"{exchange} wallet",
        "wallet_type": "exchange",
        "type_value": exchange,
        "api_key": "testapikey123456",
        "api_secret": "testapisecret123456",
    }
    if passphrase:
        body["api_passphrase"] = "testpassphrase"
    return client.post("/api/wallets", json=body, headers=auth)


def _chain(client, auth, chain, address=None, name=None):
    addr_map = {
        "tron":     "TN3W4H6rK2ce4vX9YnFQHwKENnHjoxb3m9",
        "solana":   "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    }
    addr = address or addr_map.get(chain, "0x1234567890abcdef1234567890abcdef12345678")
    return client.post("/api/wallets", json={
        "name": name or f"{chain} wallet",
        "wallet_type": "chain",
        "type_value": chain,
        "address": addr,
    }, headers=auth)


def _perpdex(client, auth, dex, address=None, name=None):
    return client.post("/api/wallets", json={
        "name": name or f"{dex} wallet",
        "wallet_type": "perpdex",
        "type_value": dex,
        "address": address or "0x1234567890abcdef1234567890abcdef12345678",
    }, headers=auth)


# ── Options endpoint ──────────────────────────────────────────────────────────

def test_options_requires_auth(client):
    assert client.get("/api/wallets/options").status_code == 401


def test_options_returns_all_types(client, auth):
    data = client.get("/api/wallets/options", headers=auth).json()
    assert len(data["exchange_types"]) == 8
    assert len(data["chain_types"]) == 13
    assert len(data["perpdex_types"]) == 5  # includes Aster (soon)


def test_options_exchange_values(client, auth):
    data = client.get("/api/wallets/options", headers=auth).json()
    values = {e["value"] for e in data["exchange_types"]}
    assert values == {"binance", "okx", "bybit", "gate", "mexc", "kucoin", "bitget", "backpack"}


def test_options_chain_values(client, auth):
    data = client.get("/api/wallets/options", headers=auth).json()
    values = {c["value"] for c in data["chain_types"]}
    assert values == {
        "tron", "ethereum", "bsc", "polygon", "arbitrum",
        "optimism", "base", "avalanche", "zksync",
        "linea", "scroll", "mantle", "blast",
    }


def test_options_perpdex_values(client, auth):
    data = client.get("/api/wallets/options", headers=auth).json()
    values = {p["value"] for p in data["perpdex_types"]}
    assert values == {"hyperliquid", "aster", "lighter", "ethereal", "paradex"}


def test_options_aster_is_soon(client, auth):
    data = client.get("/api/wallets/options", headers=auth).json()
    aster = next(p for p in data["perpdex_types"] if p["value"] == "aster")
    assert aster.get("soon") is True
    assert aster.get("needs_api_key") is True  # uses api_key not address


def test_aster_provider_importable():
    from backend.providers.perp_dexes.aster_provider import AsterProvider
    assert AsterProvider.name == "AsterProvider"


def test_aster_wallet_not_in_active_count(client, auth):
    """Aster counts toward perp_dexes in /api/providers but is excluded from available count."""
    data = client.get("/api/providers").json()
    assert data["perp_dexes"] == 4  # Aster (soon) not counted


def test_options_passphrase_flags(client, auth):
    data = client.get("/api/wallets/options", headers=auth).json()
    pp_map = {e["value"]: e["needs_passphrase"] for e in data["exchange_types"]}
    assert pp_map["okx"] is True
    assert pp_map["kucoin"] is True
    assert pp_map["bitget"] is True
    assert pp_map["binance"] is False
    assert pp_map["bybit"] is False


# ── All 8 exchanges ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("exchange,needs_pp", [
    ("binance",  False),
    ("okx",      True),
    ("bybit",    False),
    ("gate",     False),
    ("mexc",     False),
    ("kucoin",   True),
    ("bitget",   True),
    ("backpack", False),
])
def test_create_exchange_wallet(client, auth, exchange, needs_pp):
    r = _exchange(client, auth, exchange, passphrase=needs_pp)
    assert r.status_code == 201, f"{exchange}: {r.text}"
    data = r.json()
    assert data["wallet_type"] == "exchange"
    assert data["type_value"] == exchange
    assert "****" in data["display_info"]  # key is masked


# ── All 13 chains ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("chain,address", [
    ("tron",      "TN3W4H6rK2ce4vX9YnFQHwKENnHjoxb3m9"),
    ("ethereum",  "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"),
    ("bsc",       "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"),
    ("polygon",   "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"),
    ("arbitrum",  "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"),
    ("optimism",  "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"),
    ("base",      "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"),
    ("avalanche", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"),
    ("zksync",    "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"),
    ("linea",     "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"),
    ("scroll",    "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"),
    ("mantle",    "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"),
    ("blast",     "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"),
])
def test_create_chain_wallet(client, auth, chain, address):
    r = _chain(client, auth, chain, address=address)
    assert r.status_code == 201, f"{chain}: {r.text}"
    data = r.json()
    assert data["wallet_type"] == "chain"
    assert data["type_value"] == chain
    assert data["display_info"] == address


# ── All 4 active perp DEXes (excluding Aster which is soon) ──────────────────

@pytest.mark.parametrize("dex", ["hyperliquid", "lighter", "ethereal", "paradex"])
def test_create_perpdex_wallet(client, auth, dex):
    r = _perpdex(client, auth, dex)
    assert r.status_code == 201, f"{dex}: {r.text}"
    data = r.json()
    assert data["wallet_type"] == "perpdex"
    assert data["type_value"] == dex


# ── Validation ────────────────────────────────────────────────────────────────

def test_exchange_wallet_missing_api_key(client, auth):
    r = client.post("/api/wallets", json={
        "name": "my wallet",
        "wallet_type": "exchange",
        "type_value": "binance",
        "api_secret": "secret",
    }, headers=auth)
    assert r.status_code == 422


def test_chain_wallet_missing_address(client, auth):
    r = client.post("/api/wallets", json={
        "name": "my wallet",
        "wallet_type": "chain",
        "type_value": "ethereum",
    }, headers=auth)
    assert r.status_code == 422


def test_wallet_name_too_short(client, auth):
    r = client.post("/api/wallets", json={
        "name": "short",  # < 6 chars
        "wallet_type": "chain",
        "type_value": "ethereum",
        "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    }, headers=auth)
    assert r.status_code == 422


def test_invalid_wallet_type(client, auth):
    r = client.post("/api/wallets", json={
        "name": "my wallet",
        "wallet_type": "unknown",
        "type_value": "binance",
    }, headers=auth)
    assert r.status_code == 422


# ── CRUD ──────────────────────────────────────────────────────────────────────

def test_list_wallets_empty(client, auth):
    r = client.get("/api/wallets", headers=auth)
    assert r.status_code == 200
    assert r.json() == []


def test_list_wallets_shows_created(client, auth):
    _exchange(client, auth, "binance")
    _chain(client, auth, "ethereum")
    wallets = client.get("/api/wallets", headers=auth).json()
    assert len(wallets) == 2


def test_wallet_isolated_between_users(client):
    from tests.conftest import _register
    t1 = _register(client, "user1", "u1@t.com", "pass1234")
    t2 = _register(client, "user2", "u2@t.com", "pass1234")
    _exchange(client, {"Authorization": f"Bearer {t1}"}, "binance")
    wallets = client.get("/api/wallets", headers={"Authorization": f"Bearer {t2}"}).json()
    assert wallets == []


def test_delete_wallet(client, auth):
    wid = _exchange(client, auth, "binance").json()["id"]
    r = client.delete(f"/api/wallets/{wid}", headers=auth)
    assert r.status_code == 204
    assert client.get("/api/wallets", headers=auth).json() == []


def test_delete_other_users_wallet(client):
    from tests.conftest import _register
    t1 = _register(client, "owner", "owner@t.com", "pass1234")
    t2 = _register(client, "other", "other@t.com", "pass1234")
    wid = _exchange(client, {"Authorization": f"Bearer {t1}"}, "binance").json()["id"]
    r = client.delete(f"/api/wallets/{wid}", headers={"Authorization": f"Bearer {t2}"})
    assert r.status_code == 404


# ── Wallet limit ──────────────────────────────────────────────────────────────

def test_wallet_limit_5(client, auth):
    for i in range(5):
        r = _chain(client, auth, "ethereum", name=f"wallet {i+1}")
        assert r.status_code == 201, f"Wallet {i+1} failed: {r.text}"
    # 6th should be rejected
    r = _chain(client, auth, "ethereum", name="wallet 6")
    assert r.status_code == 402


def test_archived_wallet_not_in_active_list(client, auth):
    wid = _exchange(client, auth, "binance").json()["id"]
    client.post(f"/api/wallets/{wid}/archive", headers=auth)
    wallets = client.get("/api/wallets", headers=auth).json()
    assert all(w["id"] != wid for w in wallets)


# ── Archive ───────────────────────────────────────────────────────────────────

def test_archive_wallet(client, auth):
    wid = _exchange(client, auth, "binance").json()["id"]
    r = client.post(f"/api/wallets/{wid}/archive", headers=auth)
    assert r.status_code == 200
    archived = client.get("/api/wallets/archived", headers=auth).json()
    assert any(w["id"] == wid for w in archived)


def test_unarchive_wallet(client, auth):
    wid = _exchange(client, auth, "binance").json()["id"]
    client.post(f"/api/wallets/{wid}/archive", headers=auth)
    r = client.post(f"/api/wallets/{wid}/unarchive", headers=auth)
    assert r.status_code == 200
    wallets = client.get("/api/wallets", headers=auth).json()
    assert any(w["id"] == wid for w in wallets)


def test_unarchive_blocked_when_at_limit(client, auth):
    # Fill active slots
    ids = []
    for i in range(5):
        ids.append(_exchange(client, auth, "binance", name=f"wallet {i+1}").json()["id"])
    # Archive one, then create another to fill the gap
    client.post(f"/api/wallets/{ids[0]}/archive", headers=auth)
    _exchange(client, auth, "bybit", name="bybit wallet")
    # Now at 5 active again → unarchive should fail
    r = client.post(f"/api/wallets/{ids[0]}/unarchive", headers=auth)
    assert r.status_code == 402


def test_archived_list_empty_initially(client, auth):
    r = client.get("/api/wallets/archived", headers=auth)
    assert r.status_code == 200
    assert r.json() == []


# ── WalletOut fields ──────────────────────────────────────────────────────────

def test_wallet_out_has_required_fields(client, auth):
    data = _exchange(client, auth, "binance").json()
    for field in ("id", "name", "wallet_type", "type_value", "display_info", "created_at", "tags", "is_archived"):
        assert field in data, f"Missing field: {field}"


def test_wallet_credentials_never_exposed(client, auth):
    data = _exchange(client, auth, "binance").json()
    import json
    text = json.dumps(data)
    assert "testapikey123456" not in text
    assert "testapisecret123456" not in text
