"""Binance Alpha venue — token-list helper, portfolio balance, spot fetcher,
trade adapter, price-cache overlay.

All mock-based; no live API calls.
"""
from __future__ import annotations

import asyncio

import pytest


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


_TOKEN_LIST = {
    "code": "000000",
    "success": True,
    "data": [
        {"symbol": "GUA", "chainName": "BSC", "contractAddress": "0xabc",
         "price": "0.058", "volume24h": "1500000", "liquidity": "1420000",
         "fullyDelisted": False},
        {"symbol": "GUA", "chainName": "Solana", "contractAddress": "So1111",
         "price": "0.057", "volume24h": "90000", "liquidity": "50000",
         "fullyDelisted": False},
        {"symbol": "DEAD", "chainName": "BSC", "contractAddress": "0xdead",
         "price": "1.0", "volume24h": "10", "liquidity": "10",
         "fullyDelisted": True},
        {"symbol": "ZEROVOL", "chainName": "BSC", "contractAddress": "0xf00",
         "price": "2.5", "volume24h": "0", "liquidity": "8000",
         "fullyDelisted": False},
    ],
}


class _TokenListHTTP:
    async def get(self, url, **kw):
        assert "alpha/all/token/list" in url
        return _Resp(_TOKEN_LIST)


def _reset_alpha_cache(monkeypatch):
    import backend.providers.exchanges.binance_alpha_provider as mod
    monkeypatch.setattr(mod, "_alpha_cache", ({}, 0.0))


# ── Token-list helper ─────────────────────────────────────────────────────────

def test_alpha_tokens_dedupe_and_delist_filter(monkeypatch):
    from backend.providers.exchanges.binance_alpha_provider import get_alpha_tokens
    _reset_alpha_cache(monkeypatch)

    tokens = _run(get_alpha_tokens(_TokenListHTTP()))
    assert set(tokens) == {"GUA", "ZEROVOL"}
    # multi-chain dupe collapses to deepest liquidity
    assert tokens["GUA"]["price"] == pytest.approx(0.058)
    assert tokens["GUA"]["volume_usd"] == pytest.approx(1500000)


# ── Portfolio provider ────────────────────────────────────────────────────────

def test_provider_registered():
    from backend.providers.exchanges import EXCHANGE_PROVIDERS, BinanceAlphaProvider
    from backend.domain.enums import ExchangeType
    assert EXCHANGE_PROVIDERS["binancealpha"] is BinanceAlphaProvider
    assert ExchangeType("binancealpha").value == "binancealpha"
    assert BinanceAlphaProvider.needs_passphrase is False
    assert BinanceAlphaProvider.needs_api_key is True


def test_fetch_balance_filters_to_alpha_holdings(monkeypatch):
    from backend.providers.exchanges.binance_alpha_provider import BinanceAlphaProvider
    from backend.domain.models import ExchangeWallet
    _reset_alpha_cache(monkeypatch)

    account = {
        "balances": [
            {"asset": "GUA", "free": "1000", "locked": "500"},
            {"asset": "USDT", "free": "250", "locked": "0"},
            {"asset": "BTC", "free": "0.5", "locked": "0"},
            {"asset": "ZEROVOL", "free": "0", "locked": "0"},
        ]
    }

    p = BinanceAlphaProvider()

    async def fake_get(url, **kw):
        if "/api/v3/account" in url:
            assert kw["headers"]["X-MBX-APIKEY"] == "AK"
            assert "signature=" in url
            return _Resp(account)
        return _Resp(_TOKEN_LIST)

    monkeypatch.setattr(p._http, "get", fake_get)

    async def noop_aclose():
        return None

    monkeypatch.setattr(p, "aclose", noop_aclose)

    w = ExchangeWallet(name="t", user="u", exchange="binancealpha", api_key="AK", api_secret="SK")
    res = _run(p.fetch_balance(w))
    assert res.totals == {"GUA": "1500"}
    assert res.details["futures"] == {}

    # Alpha prices land in the shared cache as a fallback overlay
    from backend.services import price_service
    assert price_service.get_price("GUA") == pytest.approx(0.058)


# ── Spot screener fetcher ─────────────────────────────────────────────────────

def test_spot_fetcher_emits_alpha_rows(monkeypatch):
    import backend.services.spot_arbitrage_service as svc
    _reset_alpha_cache(monkeypatch)
    monkeypatch.setattr(svc, "_http", _TokenListHTTP())

    rows = _run(svc._fetch_binancealpha_spot())
    assert rows == [{"symbol": "GUA", "price": 0.058, "volume_usd": 1500000.0}]
    assert svc.SPOT_FETCHERS["binancealpha"] is svc._fetch_binancealpha_spot
    assert svc.SPOT_FEES["binancealpha"] == 0.001


# ── Trade adapter ─────────────────────────────────────────────────────────────

def test_adapter_registered_and_spot_native():
    from backend.services.trade_adapters import ADAPTERS, TRADE_SUPPORTED, BinanceAlphaAdapter
    assert ADAPTERS["binancealpha"] is BinanceAlphaAdapter
    assert "binancealpha" in TRADE_SUPPORTED
    assert BinanceAlphaAdapter.spot_native is True


def test_adapter_market_buy_places_spot_order(monkeypatch):
    import backend.services.trade_adapters.binance_alpha as mod
    from backend.services.trade_adapters.binance import BinanceAdapter

    calls = {}

    async def fake_signed(creds, method, path, params=None, spot_host=None):
        calls.update(method=method, path=path, params=params, spot_host=spot_host)
        return {
            "orderId": 42,
            "fills": [
                {"qty": "500", "price": "0.058"},
                {"qty": "500", "price": "0.060"},
            ],
        }

    async def fake_filters(sym):
        assert sym == "GUAUSDT"
        return {"step": 1.0, "precision": 0}

    monkeypatch.setattr(BinanceAdapter, "_signed", fake_signed)
    monkeypatch.setattr(mod, "_spot_filters", fake_filters)

    res = _run(mod.BinanceAlphaAdapter.place_order(
        {"api_key": "a", "api_secret": "b"}, "GUA", "buy", 1000.4))
    assert calls["method"] == "POST"
    assert calls["path"] == "/api/v3/order"
    assert calls["spot_host"] == mod.SPOT_BASE
    assert calls["params"]["symbol"] == "GUAUSDT"
    assert calls["params"]["side"] == "BUY"
    assert calls["params"]["type"] == "MARKET"
    assert calls["params"]["quantity"] == "1000"
    assert res["order_id"] == "42"
    assert res["avg_price"] == pytest.approx(0.059)
    assert res["filled_qty"] == pytest.approx(1000.0)


def test_adapter_close_sells_full_balance(monkeypatch):
    import backend.services.trade_adapters.binance_alpha as mod
    from backend.services.trade_adapters.binance import BinanceAdapter

    placed = {}

    async def fake_signed(creds, method, path, params=None, spot_host=None):
        assert path == "/api/v3/account"
        return {"balances": [{"asset": "GUA", "free": "725.0", "locked": "0"}]}

    async def fake_place(creds, symbol, side, quantity, **kw):
        placed.update(symbol=symbol, side=side, quantity=quantity)
        return {"order_id": "close-1", "avg_price": 0.058, "filled_qty": 725.0}

    monkeypatch.setattr(BinanceAdapter, "_signed", fake_signed)
    monkeypatch.setattr(mod.BinanceAlphaAdapter, "place_order", fake_place)

    res = _run(mod.BinanceAlphaAdapter.close_position(
        {"api_key": "a", "api_secret": "b"}, "GUA", "buy"))
    assert placed == {"symbol": "GUA", "side": "sell", "quantity": 725.0}
    assert res["order_id"] == "close-1"
    assert res["closed_qty"] == pytest.approx(725.0)


def test_adapter_list_positions_empty():
    from backend.services.trade_adapters.binance_alpha import BinanceAlphaAdapter
    assert _run(BinanceAlphaAdapter.list_positions({})) == []
