"""Portfolio: balance + transactions endpoints with mocked providers."""
from unittest.mock import AsyncMock, patch, MagicMock
from decimal import Decimal

import pytest

from backend.domain.models import BalanceResult, ExchangeWallet, ChainWallet, PerpDexWallet


# ── Helpers ───────────────────────────────────────────────────────────────────

_PASSPHRASE_VENUES = {"okx", "kucoin", "bitget"}


def _make_wallet(client, auth, wtype="exchange", tval="binance"):
    if wtype == "exchange":
        body = {"name": f"{tval} wallet", "wallet_type": "exchange",
                "type_value": tval, "api_key": "key123", "api_secret": "secret123"}
        if tval in _PASSPHRASE_VENUES:
            body["api_passphrase"] = "test-passphrase"
    elif wtype == "chain":
        body = {"name": f"{tval} wallet", "wallet_type": "chain",
                "type_value": tval, "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}
    elif wtype == "perpdex":
        body = {"name": f"{tval} wallet", "wallet_type": "perpdex",
                "type_value": tval, "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}
        if tval == "paradex":
            body["api_token"] = "dummy-paradex-jwt-for-tests"
    else:
        body = {}
    r = client.post("/api/wallets", json=body, headers=auth)
    assert r.status_code == 201, f"create {wtype}/{tval} failed: {r.status_code} {r.text}"
    return r.json()


def _fake_result(wallet_obj, totals=None):
    return (BalanceResult(
        wallet=wallet_obj,
        provider="mock",
        totals=totals or {"USDT": "1000.00"},
        details={},
    ), None, None)


# ── Balance ───────────────────────────────────────────────────────────────────

def test_balance_empty_wallet_ids_fetches_all(client, auth):
    """Empty wallet_ids → fetches all user wallets."""
    _make_wallet(client, auth, "exchange", "binance")
    with patch("backend.services.balance_service._fetch_single", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = (None, "mocked", "unknown")
        r = client.post("/api/portfolio/balance", json={"wallet_ids": []}, headers=auth)
    assert r.status_code == 200
    assert mock_fetch.called


def test_balance_requires_auth(client):
    r = client.post("/api/portfolio/balance", json={"wallet_ids": []})
    assert r.status_code == 401


def test_balance_specific_wallet_ids(client, auth):
    w1 = _make_wallet(client, auth, "exchange", "binance")
    w2 = _make_wallet(client, auth, "chain", "ethereum")
    with patch("backend.services.balance_service._fetch_single", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = (None, "mocked", "unknown")
        r = client.post("/api/portfolio/balance",
                        json={"wallet_ids": [w1["id"]]}, headers=auth)
    assert r.status_code == 200
    data = r.json()
    # Only one wallet was requested
    assert len(data["results"]) == 1
    assert data["results"][0]["wallet_id"] == w1["id"]


def test_balance_response_structure(client, auth):
    _make_wallet(client, auth, "exchange", "binance")
    with patch("backend.services.balance_service._fetch_single", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = (None, "test error", "unknown")
        r = client.post("/api/portfolio/balance", json={"wallet_ids": []}, headers=auth)
    data = r.json()
    assert "results" in data
    assert "aggregated" in data
    assert "stable" in data["aggregated"]
    assert "other" in data["aggregated"]
    assert "stable_total" in data["aggregated"]


def test_balance_provider_error_doesnt_crash_others(client, auth):
    """return_exceptions=True: one provider failure should not affect others."""
    _make_wallet(client, auth, "exchange", "binance")
    _make_wallet(client, auth, "chain", "ethereum")

    call_count = 0

    async def side_effect(wallet):
        nonlocal call_count
        call_count += 1
        if wallet.wallet_type == "exchange":
            return None, "API error", "unknown"
        return None, "RPC error", "unknown"

    # `new_callable=AsyncMock` ensures the patched callable IS a coroutine
    # function, so caller `await` works. `side_effect=async_func` alone made
    # a MagicMock whose __call__ couldn't be awaited reliably — previous
    # version of this test hung CI on `asyncio.gather`.
    with patch("backend.services.balance_service._fetch_single",
               new_callable=AsyncMock, side_effect=side_effect):
        r = client.post("/api/portfolio/balance", json={"wallet_ids": []}, headers=auth)

    assert r.status_code == 200
    assert call_count == 2
    results = r.json()["results"]
    assert all(row["error"] is not None for row in results)


def test_balance_aggregates_stablecoins(client, auth):
    w = _make_wallet(client, auth, "exchange", "binance")

    async def mock_fetch(wallet):
        obj = MagicMock()
        obj.totals = {"USDT": "500.00", "USDC": "300.00"}
        obj.details = {}
        result = BalanceResult(wallet=MagicMock(), provider="mock",
                               totals={"USDT": "500.00", "USDC": "300.00"}, details={})
        return result, None, None

    with patch("backend.services.balance_service._fetch_single", side_effect=mock_fetch):
        r = client.post("/api/portfolio/balance", json={"wallet_ids": []}, headers=auth)

    data = r.json()
    agg = data["aggregated"]
    assert "USDT" in agg["stable"] or "USDC" in agg["stable"]
    assert float(agg["stable_total"]) == pytest.approx(800.0, abs=0.01)


# ── Transactions ──────────────────────────────────────────────────────────────

def test_transactions_requires_auth(client):
    r = client.post("/api/portfolio/transactions", json={"wallet_id": 1})
    assert r.status_code == 401


def test_transactions_wrong_wallet_id(client, auth):
    r = client.post("/api/portfolio/transactions", json={"wallet_id": 99999}, headers=auth)
    assert r.status_code in (404, 200)  # 200 with error field is also acceptable


def test_transactions_exchange_wallet(client, auth):
    w = _make_wallet(client, auth, "exchange", "binance")
    with patch("backend.services.transaction_service.fetch_transactions",
               new_callable=AsyncMock) as mock_tx:
        mock_tx.return_value = MagicMock(
            transactions=[],
            error=None,
        )
        r = client.post("/api/portfolio/transactions",
                        json={"wallet_id": w["id"]}, headers=auth)
    assert r.status_code == 200


def test_transactions_response_structure(client, auth):
    w = _make_wallet(client, auth, "chain", "ethereum")
    with patch("backend.services.transaction_service.fetch_transactions",
               new_callable=AsyncMock) as mock_tx:
        mock_tx.return_value = MagicMock(transactions=[], error=None)
        r = client.post("/api/portfolio/transactions",
                        json={"wallet_id": w["id"]}, headers=auth)
    data = r.json()
    assert "transactions" in data or "error" in data


# ── Provider routing (smoke tests per type) ───────────────────────────────────

@pytest.mark.parametrize("exchange", [
    "binance", "okx", "bybit", "gate", "mexc", "kucoin", "bitget", "backpack"
])
def test_balance_accepts_all_exchanges(client, auth, exchange):
    w = _make_wallet(client, auth, "exchange", exchange)
    with patch("backend.services.balance_service._fetch_single", new_callable=AsyncMock) as m:
        m.return_value = (None, "mocked", "unknown")
        r = client.post("/api/portfolio/balance",
                        json={"wallet_ids": [w["id"]]}, headers=auth)
    assert r.status_code == 200
    assert r.json()["results"][0]["type_value"] == exchange


@pytest.mark.parametrize("chain,addr", [
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
def test_balance_accepts_all_chains(client, auth, chain, addr):
    w = client.post("/api/wallets", json={
        "name": f"{chain} wallet", "wallet_type": "chain",
        "type_value": chain, "address": addr,
    }, headers=auth).json()
    with patch("backend.services.balance_service._fetch_single", new_callable=AsyncMock) as m:
        m.return_value = (None, "mocked", "unknown")
        r = client.post("/api/portfolio/balance",
                        json={"wallet_ids": [w["id"]]}, headers=auth)
    assert r.status_code == 200
    assert r.json()["results"][0]["type_value"] == chain


@pytest.mark.parametrize("dex", ["hyperliquid", "lighter", "ethereal", "paradex"])
def test_balance_accepts_all_perpdexes(client, auth, dex):
    w = _make_wallet(client, auth, "perpdex", dex)
    with patch("backend.services.balance_service._fetch_single", new_callable=AsyncMock) as m:
        m.return_value = (None, "mocked", "unknown")
        r = client.post("/api/portfolio/balance",
                        json={"wallet_ids": [w["id"]]}, headers=auth)
    assert r.status_code == 200
    assert r.json()["results"][0]["type_value"] == dex
