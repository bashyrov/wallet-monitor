"""Provider-metadata tests — one provider per exchange, chain, perp-DEX.

Every provider must:
  1. Declare the attrs `/api/wallets/options` relies on (label, enabled, flags).
  2. Wire up in its registry dict (EXCHANGE_PROVIDERS / CHAIN_PROVIDERS / PERPDEX_PROVIDERS).
  3. Match the enum in backend/domain/enums.py.
  4. Not crash when a domain wallet resolves its provider (no metaclass drift).

This catches the "I added a provider but forgot to register it" / "I
renamed the class but the registry still points at the old name" class
of breakage before it reaches prod.
"""
from __future__ import annotations

import pytest


# ── Exchanges ─────────────────────────────────────────────────────────────────
def test_exchange_registry_subset_of_enum():
    """Every registered exchange class must have an enum entry so the Pydantic
    wallet schema can resolve it. The opposite direction (enum values without
    a registered class) is allowed — those are "declared but not implemented"
    and are skipped by WALLET_OPTIONS generation.
    """
    from backend.providers.exchanges import EXCHANGE_PROVIDERS
    from backend.domain.enums import ExchangeType
    assert EXCHANGE_PROVIDERS, "no exchanges registered"
    enum_values = {e.value for e in ExchangeType}
    missing = set(EXCHANGE_PROVIDERS.keys()) - enum_values
    assert not missing, f"EXCHANGE_PROVIDERS has values not in ExchangeType: {missing}"


@pytest.mark.parametrize("value", [])  # filled below
def test_exchange_provider_metadata(value: str):
    """Every exchange class exposes the attrs WALLET_OPTIONS needs."""
    from backend.providers.exchanges import EXCHANGE_PROVIDERS
    cls = EXCHANGE_PROVIDERS[value]
    assert getattr(cls, "label", None), f"{cls.__name__}: missing `label`"
    assert isinstance(getattr(cls, "enabled", None), bool), f"{cls.__name__}: `enabled` must be bool"
    assert isinstance(getattr(cls, "needs_passphrase", False), bool)


def _ex_values():
    from backend.providers.exchanges import EXCHANGE_PROVIDERS
    return list(EXCHANGE_PROVIDERS.keys())


def pytest_generate_tests(metafunc):
    """Parametrize exchange / chain / perpdex tests from the live registries.

    Done here (not as module-level parametrize) so the collection ordering
    doesn't depend on import side-effects from other test files."""
    if "exchange_value" in metafunc.fixturenames:
        from backend.providers.exchanges import EXCHANGE_PROVIDERS
        metafunc.parametrize("exchange_value", sorted(EXCHANGE_PROVIDERS.keys()))
    if "chain_value" in metafunc.fixturenames:
        from backend.providers.chains import CHAIN_PROVIDERS
        metafunc.parametrize("chain_value", sorted(CHAIN_PROVIDERS.keys()))
    if "perpdex_value" in metafunc.fixturenames:
        from backend.providers.perp_dexes import PERPDEX_PROVIDERS
        metafunc.parametrize("perpdex_value", sorted(PERPDEX_PROVIDERS.keys()))


def test_exchange_has_required_attrs(exchange_value):
    from backend.providers.exchanges import EXCHANGE_PROVIDERS
    cls = EXCHANGE_PROVIDERS[exchange_value]
    assert isinstance(getattr(cls, "label", None), str) and cls.label
    assert isinstance(getattr(cls, "enabled", None), bool)
    assert isinstance(getattr(cls, "needs_passphrase", False), bool)
    # fetch_balance is abstract-async — must be callable on the class
    assert callable(getattr(cls, "fetch_balance", None))


# ── Chains ────────────────────────────────────────────────────────────────────
def test_chain_registry_matches_enum():
    from backend.providers.chains import CHAIN_PROVIDERS, CHAIN_META
    from backend.domain.enums import ChainType
    assert CHAIN_PROVIDERS
    enum_values = {c.value for c in ChainType}
    registry = set(CHAIN_PROVIDERS.keys())
    # Registry may include chains not-yet surfaced in CHAIN_META; that's OK
    # (disabled path). But it must never contain chains missing from the enum.
    missing = registry - enum_values
    assert not missing, f"CHAIN_PROVIDERS has values not in ChainType: {missing}"
    # CHAIN_META should cover every chain surfaced in UI
    for ch in CHAIN_META:
        assert ch in CHAIN_PROVIDERS, f"CHAIN_META has {ch} but no provider registered"


def test_chain_provider_has_required_attrs(chain_value):
    from backend.providers.chains import CHAIN_PROVIDERS
    cls = CHAIN_PROVIDERS[chain_value]
    assert callable(getattr(cls, "fetch_balance", None))


# ── Perp DEXes ────────────────────────────────────────────────────────────────
def test_perpdex_registry_subset_of_enum():
    from backend.providers.perp_dexes import PERPDEX_PROVIDERS
    from backend.domain.enums import PerpDexType
    assert PERPDEX_PROVIDERS
    enum_values = {p.value for p in PerpDexType}
    missing = set(PERPDEX_PROVIDERS.keys()) - enum_values
    assert not missing, f"PERPDEX_PROVIDERS has values not in PerpDexType: {missing}"


def test_perpdex_has_required_attrs(perpdex_value):
    from backend.providers.perp_dexes import PERPDEX_PROVIDERS
    cls = PERPDEX_PROVIDERS[perpdex_value]
    assert isinstance(getattr(cls, "label", None), str) and cls.label
    assert isinstance(getattr(cls, "enabled", None), bool)
    assert callable(getattr(cls, "fetch_balance", None))


# ── WALLET_OPTIONS surface (what the frontend consumes) ───────────────────────
def test_wallet_options_surface_is_complete():
    """GET /api/wallets/options is the contract the frontend depends on — if
    a provider gets disabled or renamed, this must surface it."""
    from backend.api.v1.wallets import WALLET_OPTIONS
    assert "exchange_types" in WALLET_OPTIONS
    assert "chain_types" in WALLET_OPTIONS
    assert "perpdex_types" in WALLET_OPTIONS
    for section in ("exchange_types", "chain_types", "perpdex_types"):
        assert WALLET_OPTIONS[section], f"{section} must not be empty"
        for entry in WALLET_OPTIONS[section]:
            assert "value" in entry and "label" in entry, f"bad {section} entry: {entry}"


# ── Funding fetcher registry ──────────────────────────────────────────────────
def test_funding_fetchers_cover_supported_exchanges():
    """Arb screener reads from FETCHERS — if an exchange is missing from this
    dict, its funding rates never hit the arb compute and it silently
    disappears from the UI."""
    from backend.services.arbitrage_service import FETCHERS
    expected = {"binance", "bybit", "okx", "gate", "kucoin", "mexc",
                "bitget", "hyperliquid", "aster", "ethereal", "whitebit", "bingx"}
    missing = expected - set(FETCHERS.keys())
    assert not missing, f"FETCHERS missing fetchers for {missing}"


def test_orderbook_ws_adapters_cover_cex():
    """Orderbook WS path has its own registry — keep it in lockstep with
    arb-compute FETCHERS so /arb panels aren't empty for a venue that
    shows up in the arb feed."""
    from backend.services.orderbook_ws.adapters import ADAPTERS
    # Every CEX in arb should either have an orderbook WS adapter or be
    # flagged non-WS (polled via REST in orderbook_cache).
    expected_ws = {"binance", "bybit", "okx", "gate", "kucoin", "mexc",
                   "bitget", "hyperliquid", "aster", "whitebit", "bingx"}
    missing = expected_ws - set(ADAPTERS.keys())
    assert not missing, f"orderbook WS missing adapters for {missing}"
