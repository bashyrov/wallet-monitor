"""Paradex integration — screener fetcher + wallet schema."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from backend.domain.enums import PerpDexType


# ── Screener fetcher ────────────────────────────────────────────────────────


def _mock_response(json_body: dict):
    class _R:
        status_code = 200
        def json(self): return json_body
        def raise_for_status(self): pass
    return _R()


def test_paradex_fetcher_filters_to_perp_only():
    """Only -USD-PERP symbols should be surfaced; options/futures/other
    asset kinds (~500 of the 600 markets on Paradex) must be dropped."""
    from backend.services import arbitrage_service as arb

    body = {
        "results": [
            # Real perp — keep.
            {
                "symbol": "BTC-USD-PERP",
                "mark_price": "70000.1",
                "funding_rate": "0.00005",
                "volume_24h": "1000",  # base units; fetcher converts to USD
            },
            # Options — drop.
            {
                "symbol": "BTC-USD-29MAY26-310000-C",
                "mark_price": "50.0",
                "funding_rate": "0",
                "volume_24h": "10",
            },
            # Another perp — keep.
            {
                "symbol": "SOL-USD-PERP",
                "mark_price": "85.3",
                "funding_rate": "-0.00002",
                "volume_24h": "500",
            },
            # Malformed — drop.
            {"symbol": "", "mark_price": "1", "funding_rate": "0.1"},
        ]
    }
    with patch.object(arb, "_http") as m_http:
        m_http.get = AsyncMock(return_value=_mock_response(body))
        rows = asyncio.run(arb._fetch_paradex())

    symbols = {r["symbol"] for r in rows}
    assert symbols == {"BTC", "SOL"}
    for r in rows:
        assert r["exchange"] == "paradex"
        assert r["interval_h"] == 8.0
        assert r["price"] > 0
        assert r["rate"] != 0


def test_paradex_fetcher_drops_zero_price_or_zero_rate():
    """Same-mark-price-as-zero or zero funding are uninformative for arb."""
    from backend.services import arbitrage_service as arb

    body = {
        "results": [
            {"symbol": "ZER-USD-PERP", "mark_price": "0", "funding_rate": "0.1", "volume_24h": "1"},
            {"symbol": "FLT-USD-PERP", "mark_price": "1", "funding_rate": "0", "volume_24h": "1"},
            {"symbol": "OK-USD-PERP",  "mark_price": "1", "funding_rate": "0.01", "volume_24h": "1"},
        ]
    }
    with patch.object(arb, "_http") as m_http:
        m_http.get = AsyncMock(return_value=_mock_response(body))
        rows = asyncio.run(arb._fetch_paradex())

    assert {r["symbol"] for r in rows} == {"OK"}


def test_paradex_fetcher_volume_converted_to_usd():
    """Paradex reports `volume_24h` in base asset units; we want USD for
    the cross-venue arb filter to apply consistently."""
    from backend.services import arbitrage_service as arb

    body = {
        "results": [
            {"symbol": "BTC-USD-PERP", "mark_price": "70000", "funding_rate": "0.0001", "volume_24h": "10"},
        ]
    }
    with patch.object(arb, "_http") as m_http:
        m_http.get = AsyncMock(return_value=_mock_response(body))
        rows = asyncio.run(arb._fetch_paradex())

    assert len(rows) == 1
    # 10 BTC * $70000 = $700_000 expected volume_usd
    assert rows[0]["volume_usd"] == pytest.approx(700_000)


def test_paradex_in_main_FETCHERS_registry():
    """Dispatcher should route 'paradex' to our fetcher."""
    from backend.services.arbitrage_service import FETCHERS, _fetch_paradex

    assert "paradex" in FETCHERS
    assert FETCHERS["paradex"] is _fetch_paradex


def test_paradex_fee_configured():
    """Fee must be set so screener compute doesn't fall through to default."""
    from backend.services.arbitrage_service import EXCHANGE_FEES

    assert "paradex" in EXCHANGE_FEES
    assert EXCHANGE_FEES["paradex"] > 0
    assert EXCHANGE_FEES["paradex"] < 0.01   # sanity — no fat-fingered 10% fee


# ── Wallet schema ──────────────────────────────────────────────────────────


def test_perpdex_schema_requires_api_token_for_paradex():
    from pydantic import ValidationError
    from backend.schemas.wallets import PerpDexWalletSchema

    # Missing api_token → should fail.
    with pytest.raises(ValidationError) as exc:
        PerpDexWalletSchema(
            name="myparadex1", user="u",
            perp_dex=PerpDexType.PARADEX,
            address="0x01a2b3c4d5e6f789",
        )
    assert "api_token" in str(exc.value).lower() or "paradex" in str(exc.value).lower()


def test_perpdex_schema_accepts_paradex_with_token():
    from backend.schemas.wallets import PerpDexWalletSchema

    w = PerpDexWalletSchema(
        name="myparadex1", user="u",
        perp_dex=PerpDexType.PARADEX,
        address="0x01a2b3c4d5e6f789",
        api_token="eyJhbGciOi" + "x" * 50,
    )
    assert w.api_token is not None


def test_perpdex_schema_hyperliquid_ignores_api_token():
    """Non-Paradex perpdex shouldn't require a token. Missing is fine."""
    from backend.schemas.wallets import PerpDexWalletSchema

    w = PerpDexWalletSchema(
        name="myhypertest1", user="u",
        perp_dex=PerpDexType.HYPERLIQUID,
        address="0x1234567890abcdef1234567890abcdef12345678",
    )
    assert w.api_token is None


# ── Provider flag for UI ────────────────────────────────────────────────────


def test_paradex_provider_exposes_needs_api_token():
    from backend.providers.perp_dexes.paradex_provider import ParadexProvider

    assert getattr(ParadexProvider, "needs_api_token", False) is True


def test_perpdex_providers_registry_includes_paradex():
    from backend.providers.perp_dexes import PERPDEX_PROVIDERS

    assert "paradex" in PERPDEX_PROVIDERS
    prov = PERPDEX_PROVIDERS["paradex"]
    assert getattr(prov, "enabled", False) is True
    assert getattr(prov, "label", None) == "Paradex"
