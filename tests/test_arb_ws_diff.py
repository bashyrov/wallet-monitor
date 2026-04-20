"""Unit tests for the arb WS diff protocol.

Covers:
  · _arb_key identity
  · _opps_differ field-level detection
  · _build_arb_diff: added / updated / removed branches
  · _build_arb_diff: no-op returns None
  · _build_arb_diff: meta (fees / exchanges) change triggers push
  · snapshot payload shape
  · bandwidth-vs-snapshot measurement (informational)

Run from project root:
    python3 -m pytest tests/test_arb_ws_diff.py -v
"""
from __future__ import annotations

import json
import sys
import pytest

from backend.api.v1 import screener as S


@pytest.fixture(autouse=True)
def _reset_state():
    """Every test starts with a clean diff-state dict so ordering doesn't
    pollute across cases."""
    S._last_arb_broadcast = {}
    S._last_arb_meta = {"ts": 0.0, "fees": {}, "exchanges": []}
    yield
    S._last_arb_broadcast = {}
    S._last_arb_meta = {"ts": 0.0, "fees": {}, "exchanges": []}


def _opp(sym, long_ex, short_ex, **kwargs):
    """Minimal opp dict with the fields the diff cares about."""
    base = {
        "symbol": sym,
        "long_exchange": long_ex,
        "short_exchange": short_ex,
        "net_profit": 1.0,
        "gross_funding": 0.5,
        "price_spread": 0.5,
        "total_fees": 0.1,
        "long_price": 100.0,
        "short_price": 100.5,
        "long_rate": 0.0001,
        "short_rate": 0.0002,
        "long_volume": 1_000_000,
        "short_volume": 2_000_000,
        "next_ts_long": 1700000000,
        "next_ts_short": 1700000000,
        "valid_price": True,
    }
    base.update(kwargs)
    return base


# ── _arb_key / _opps_differ ─────────────────────────────────────────────────

def test_arb_key_tuple():
    o = _opp("BTC", "binance", "okx")
    assert S._arb_key(o) == ("BTC", "binance", "okx")


def test_opps_differ_detects_price_change():
    a = _opp("BTC", "binance", "okx", long_price=100.0)
    b = _opp("BTC", "binance", "okx", long_price=100.5)
    assert S._opps_differ(a, b) is True


def test_opps_differ_ignores_apr_not_in_fields():
    a = _opp("BTC", "binance", "okx")
    b = _opp("BTC", "binance", "okx")
    b["apr"] = 9999  # not in _ARB_DIFF_FIELDS
    assert S._opps_differ(a, b) is False


def test_opps_differ_detects_net_profit_change():
    a = _opp("BTC", "binance", "okx", net_profit=1.0)
    b = _opp("BTC", "binance", "okx", net_profit=1.01)
    assert S._opps_differ(a, b) is True


# ── _build_arb_diff: first call = everything is "added" ─────────────────────

def test_first_call_everything_is_added():
    curr = {"ts": 1, "fees": {}, "exchanges": ["binance"], "opportunities": [
        _opp("BTC", "binance", "okx"),
        _opp("ETH", "binance", "okx"),
    ]}
    d = S._build_arb_diff(curr)
    assert d is not None
    assert d["type"] == "diff"
    assert len(d["added"]) == 2
    assert "updated" not in d
    assert "removed" not in d


# ── Repeat call with identical data: returns None (no push) ─────────────────

def test_identical_data_returns_none():
    curr = {"ts": 1, "fees": {"binance": 0.04}, "exchanges": ["binance"], "opportunities": [
        _opp("BTC", "binance", "okx"),
    ]}
    first = S._build_arb_diff(curr)
    assert first is not None
    # Same data on next tick — nothing to push.
    second = S._build_arb_diff(curr)
    assert second is None


# ── Field change produces "updated" ─────────────────────────────────────────

def test_field_change_goes_to_updated():
    o1 = _opp("BTC", "binance", "okx", net_profit=1.0)
    o2 = _opp("BTC", "binance", "okx", net_profit=1.2)
    S._build_arb_diff({"ts": 1, "fees": {}, "exchanges": [], "opportunities": [o1]})
    d = S._build_arb_diff({"ts": 2, "fees": {}, "exchanges": [], "opportunities": [o2]})
    assert d is not None
    assert len(d.get("updated", [])) == 1
    assert d["updated"][0]["net_profit"] == 1.2
    assert "added" not in d
    assert "removed" not in d


# ── Row disappearing = "removed" ────────────────────────────────────────────

def test_dropped_row_goes_to_removed():
    o1 = _opp("BTC", "binance", "okx")
    o2 = _opp("ETH", "binance", "okx")
    S._build_arb_diff({"ts": 1, "fees": {}, "exchanges": [], "opportunities": [o1, o2]})
    # Next tick, ETH fell out of the top-N
    d = S._build_arb_diff({"ts": 2, "fees": {}, "exchanges": [], "opportunities": [o1]})
    assert d is not None
    assert "removed" in d
    assert any(k == ["ETH", "binance", "okx"] for k in d["removed"])
    assert "added" not in d
    assert "updated" not in d


# ── Fees dict change triggers meta push ─────────────────────────────────────

def test_fees_change_triggers_meta_push():
    curr_1 = {"ts": 1, "fees": {"binance": 0.04}, "exchanges": ["binance"],
              "opportunities": [_opp("BTC", "binance", "okx")]}
    S._build_arb_diff(curr_1)
    # Same opps, different fees — should still send a diff
    curr_2 = {"ts": 2, "fees": {"binance": 0.05}, "exchanges": ["binance"],
              "opportunities": [_opp("BTC", "binance", "okx")]}
    d = S._build_arb_diff(curr_2)
    assert d is not None
    assert d.get("fees") == {"binance": 0.05}


# ── Snapshot payload is wire-compatible with legacy consumers ──────────────

def test_snapshot_payload_includes_opportunities_and_fees():
    data = {"ts": 1, "fees": {"binance": 0.04}, "exchanges": ["binance"],
            "opportunities": [_opp("BTC", "binance", "okx")]}
    payload = json.loads(S._build_arb_snapshot_payload(data))
    assert payload["type"] == "snapshot"
    assert payload["fees"] == {"binance": 0.04}
    assert len(payload["opportunities"]) == 1
    assert payload["opportunities"][0]["symbol"] == "BTC"


# ── Realistic bandwidth comparison ──────────────────────────────────────────

def test_diff_bandwidth_vs_snapshot():
    """With a large, mostly-stable arb table (500 opps, 20 changes per tick),
    the diff should be at least 10× smaller than the full snapshot. This is
    informational — not a strict pass/fail gate, but it prints a value that
    makes the perf claim auditable."""
    # Build a 500-opp "top list"
    opps_snapshot = []
    for i in range(500):
        sym = f"T{i:04d}"
        opps_snapshot.append(_opp(sym, "binance", "okx", net_profit=1.0 + i * 0.01,
                                   long_price=100.0 + i, short_price=100.5 + i))
    data = {"ts": 1, "fees": {}, "exchanges": [], "opportunities": opps_snapshot}
    # Seed the diff state
    S._build_arb_diff(data)

    # Tick 2: 20 opps' prices nudged
    next_opps = [dict(o) for o in opps_snapshot]
    for j in range(0, 20):
        next_opps[j]["net_profit"] = opps_snapshot[j]["net_profit"] + 0.0001
    next_data = {"ts": 2, "fees": {}, "exchanges": [], "opportunities": next_opps}
    diff = S._build_arb_diff(next_data)

    snapshot_bytes = len(S._build_arb_snapshot_payload(next_data).encode())
    diff_bytes = len(json.dumps(diff).encode())
    ratio = snapshot_bytes / max(1, diff_bytes)
    print(f"\n   [bandwidth] snapshot={snapshot_bytes}B  diff={diff_bytes}B  ratio={ratio:.1f}×")
    assert ratio >= 10.0, f"diff should be ≥10× smaller than snapshot (got {ratio:.1f}×)"
