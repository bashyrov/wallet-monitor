"""Upbit venue — auth header, portfolio balance, spot fetcher, trade adapter.

All mock-based; no live API calls.
"""
from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import urlencode

import pytest
from jose import jwt


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_auth_header_no_params():
    from backend.providers.exchanges.upbit_provider import upbit_auth_headers
    h = upbit_auth_headers("AK", "SK")
    assert h["Authorization"].startswith("Bearer ")
    claims = jwt.decode(h["Authorization"][7:], "SK", algorithms=["HS256"])
    assert claims["access_key"] == "AK"
    assert claims["nonce"]
    assert "query_hash" not in claims


def test_auth_header_with_params_sha512():
    from backend.providers.exchanges.upbit_provider import upbit_auth_headers
    params = {"limit": 5, "order_by": "desc"}
    h = upbit_auth_headers("AK", "SK", params)
    claims = jwt.decode(h["Authorization"][7:], "SK", algorithms=["HS256"])
    expected = hashlib.sha512(urlencode(params, doseq=True).encode()).hexdigest()
    assert claims["query_hash"] == expected
    assert claims["query_hash_alg"] == "SHA512"


# ── Portfolio balance ─────────────────────────────────────────────────────────

def test_fetch_balance_aggregates_accounts(monkeypatch):
    from backend.providers.exchanges.upbit_provider import UpbitProvider
    from backend.domain.models import ExchangeWallet

    accounts = [
        {"currency": "USDT", "balance": "100.5", "locked": "10", "avg_buy_price": "1"},
        {"currency": "BTC", "balance": "0.5", "locked": "0", "avg_buy_price": "60000"},
        {"currency": "KRW", "balance": "50000", "locked": "0", "avg_buy_price": "0"},
        {"currency": "DUST", "balance": "0", "locked": "0", "avg_buy_price": "0"},
    ]

    p = UpbitProvider()

    async def fake_get(url, **kw):
        assert url.endswith("/v1/accounts")
        assert kw["headers"]["Authorization"].startswith("Bearer ")
        return _Resp(accounts)

    monkeypatch.setattr(p._http, "get", fake_get)
    w = ExchangeWallet(name="t", user="u", exchange="upbit", api_key="AK", api_secret="SK")
    res = _run(p.fetch_balance(w))
    assert res.totals["USDT"] == "110.5"
    assert res.totals["BTC"] == "0.5"
    assert res.totals["KRW"] == "50000"
    assert "DUST" not in res.totals
    assert res.details["futures"] == {}


def test_provider_registered():
    from backend.providers.exchanges import EXCHANGE_PROVIDERS, UpbitProvider
    from backend.domain.enums import ExchangeType
    assert EXCHANGE_PROVIDERS["upbit"] is UpbitProvider
    assert ExchangeType("upbit").value == "upbit"
    assert UpbitProvider.needs_passphrase is False
    assert UpbitProvider.needs_api_key is True


# ── Spot screener fetcher ─────────────────────────────────────────────────────

def test_spot_fetcher_parses_usdt_markets(monkeypatch):
    import backend.services.spot_arbitrage_service as svc

    markets = [{"market": m} for m in ("USDT-BTC", "USDT-DOGE", "KRW-BTC", "BTC-ETH")]
    tickers = [
        {"market": "USDT-BTC", "trade_price": 62647.48, "acc_trade_price_24h": 58409.36},
        {"market": "USDT-DOGE", "trade_price": 0.078, "acc_trade_price_24h": 12345.0},
    ]

    async def fake_get(url, params=None, **kw):
        if url.endswith("/v1/market/all"):
            return _Resp(markets)
        assert url.endswith("/v1/ticker")
        assert params["markets"] == "USDT-BTC,USDT-DOGE"
        return _Resp(tickers)

    monkeypatch.setattr(svc, "_upbit_markets_cache", ([], 0.0))
    monkeypatch.setattr(svc._http, "get", fake_get)
    rows = _run(svc._fetch_upbit_spot())
    assert rows == [
        {"symbol": "BTC", "price": 62647.48, "volume_usd": 58409.36},
        {"symbol": "DOGE", "price": 0.078, "volume_usd": 12345.0},
    ]
    assert svc.SPOT_FETCHERS["upbit"] is svc._fetch_upbit_spot
    assert svc.SPOT_FEES["upbit"] == 0.0025


# ── Trade adapter ─────────────────────────────────────────────────────────────

def test_adapter_registered_and_spot_native():
    from backend.services.trade_adapters import ADAPTERS, TRADE_SUPPORTED, UpbitAdapter
    assert ADAPTERS["upbit"] is UpbitAdapter
    assert "upbit" in TRADE_SUPPORTED
    assert UpbitAdapter.spot_native is True


def test_adapter_market_buy_uses_price_ordtype(monkeypatch):
    from backend.services.trade_adapters.upbit import UpbitAdapter

    calls = {}

    async def fake_request(cls, creds, method, path, params=None):
        if method == "POST":
            calls["order"] = params
            return {"uuid": "abc-123"}
        return {"trades": [{"funds": "125.2", "volume": "2.0"}]}

    async def fake_price(cls, creds, market):
        calls["price_market"] = market
        return 62.6

    monkeypatch.setattr(UpbitAdapter, "_request", classmethod(fake_request))
    monkeypatch.setattr(UpbitAdapter, "_last_price", classmethod(fake_price))
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    res = _run(UpbitAdapter.place_order({"api_key": "a", "api_secret": "b"}, "SOL", "buy", 2.0))
    assert calls["price_market"] == "USDT-SOL"
    assert calls["order"]["side"] == "bid"
    assert calls["order"]["ord_type"] == "price"
    assert float(calls["order"]["price"]) == pytest.approx(125.2)
    assert res["order_id"] == "abc-123"
    assert res["avg_price"] == pytest.approx(62.6)
    assert res["filled_qty"] == pytest.approx(2.0)


def test_adapter_market_sell_uses_volume(monkeypatch):
    from backend.services.trade_adapters.upbit import UpbitAdapter

    calls = {}

    async def fake_request(cls, creds, method, path, params=None):
        if method == "POST":
            calls["order"] = params
            return {"uuid": "sell-1"}
        return {"trades": []}

    monkeypatch.setattr(UpbitAdapter, "_request", classmethod(fake_request))
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    res = _run(UpbitAdapter.place_order({"api_key": "a", "api_secret": "b"}, "SOL", "sell", 3.5))
    assert calls["order"]["side"] == "ask"
    assert calls["order"]["ord_type"] == "market"
    assert float(calls["order"]["volume"]) == pytest.approx(3.5)
    assert res["order_id"] == "sell-1"


def test_adapter_close_sells_full_balance(monkeypatch):
    from backend.services.trade_adapters.upbit import UpbitAdapter

    placed = {}

    async def fake_request(cls, creds, method, path, params=None):
        assert path == "/v1/accounts"
        return [{"currency": "SOL", "balance": "7.25", "locked": "0"}]

    async def fake_place(cls, creds, symbol, side, quantity, **kw):
        placed.update(symbol=symbol, side=side, quantity=quantity)
        return {"order_id": "close-1", "avg_price": 60.0, "filled_qty": 7.25}

    monkeypatch.setattr(UpbitAdapter, "_request", classmethod(fake_request))
    monkeypatch.setattr(UpbitAdapter, "place_order", classmethod(fake_place))
    res = _run(UpbitAdapter.close_position({"api_key": "a", "api_secret": "b"}, "SOL", "buy"))
    assert placed == {"symbol": "SOL", "side": "sell", "quantity": 7.25}
    assert res["order_id"] == "close-1"
    assert res["closed_qty"] == pytest.approx(7.25)


def test_adapter_list_positions_empty():
    from backend.services.trade_adapters.upbit import UpbitAdapter
    assert _run(UpbitAdapter.list_positions({})) == []


async def _noop_sleep(_secs):
    return None
