"""Token registry — contract-address validation for the ticker-collision guard."""
from __future__ import annotations

import time

from backend.services import token_registry as tr


def _seed(registry):
    """Populate the module-level registry directly (no network)."""
    tr._registry = registry
    tr._registry_ts = time.time()
    tr._pair_verdict.clear()


def test_same_contract_different_chains_still_matches():
    _seed({
        "binance": {"FOO": {"ethereum": "0xabc123"}},
        "kucoin":  {"FOO": {"ethereum": "0xabc123"}},
    })
    assert tr.validate_pair_identity("FOO", "binance", "kucoin") is True


def test_different_contract_mismatch():
    _seed({
        "binance": {"FOO": {"ethereum": "0xdeadbeef"}},
        "kucoin":  {"FOO": {"ethereum": "0xfeedface"}},
    })
    assert tr.validate_pair_identity("FOO", "binance", "kucoin") is False


def test_unknown_exchange_returns_none():
    _seed({"kucoin": {"FOO": {"ethereum": "0xabc"}}})
    # binance not in registry → unknown
    assert tr.validate_pair_identity("FOO", "binance", "kucoin") is None


def test_unknown_symbol_returns_none():
    _seed({
        "binance": {"FOO": {"ethereum": "0xabc"}},
        "kucoin":  {"BAR": {"ethereum": "0xabc"}},
    })
    assert tr.validate_pair_identity("FOO", "binance", "kucoin") is None


def test_chain_alias_normalisation():
    """KuCoin labels Ethereum as 'ERC20'; Gate says 'eth'. Must still match."""
    assert tr._canon_chain("ERC20") == "ethereum"
    assert tr._canon_chain("eth") == "ethereum"
    assert tr._canon_chain("TRC20") == "tron"
    assert tr._canon_chain("bep20") == "bsc"


def test_pair_verdict_memoised():
    _seed({
        "binance": {"FOO": {"ethereum": "0xabc"}},
        "kucoin":  {"FOO": {"ethereum": "0xabc"}},
    })
    # First call computes
    v1 = tr.validate_pair_identity("FOO", "binance", "kucoin")
    # Second hits cache
    v2 = tr.validate_pair_identity("FOO", "binance", "kucoin")
    assert v1 is True and v2 is True
    # Symmetric key
    v3 = tr.validate_pair_identity("FOO", "kucoin", "binance")
    assert v3 is True
    # Only one entry in memo (a, b alphabetical)
    keys = list(tr._pair_verdict.keys())
    assert len(keys) == 1 and keys[0] == ("FOO", "binance", "kucoin")
