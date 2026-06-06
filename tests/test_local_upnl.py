"""Phase 1.1 — local UPNL recompute behind AVALANT_LOCAL_UPNL flag.

Unit-tests the post-process: given a position row with entry+qty+side and
a fresh live mark in arbitrage_service._cache, the override:
  mark_price → live_mark
  unrealized_pnl_usd → (mark - entry) * qty * dir
  mark_source → "live"
  mark_age_s → seconds since the screener cache last updated

Behaviour when flag off, mark stale, mark missing — leaves venue values
intact (mark_source="venue" or unset).

NOT a live verification — that needs a real position. STATUS: code-ready,
acceptance gated on creds.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-32-chars-long-aaaa")

import importlib
import pytest
from unittest.mock import patch


def _reload_with_flag(value: str):
    """Reload trade_service so the module-level flag picks up the env var."""
    os.environ["AVALANT_LOCAL_UPNL"] = value
    from backend.services import trade_service
    return importlib.reload(trade_service)


def _seed_cache(rows: list[dict], age_s: float = 0.0):
    """Drop fake rows into arbitrage_service._cache for one exchange."""
    from backend.services import arbitrage_service
    # _cache structure: {exchange: (rows, cached_at_mono)}
    bucket = arbitrage_service._cache
    by_ex: dict[str, list[dict]] = {}
    for r in rows:
        by_ex.setdefault(r["exchange"], []).append(r)
    fake_now = arbitrage_service._mono()
    for ex, items in by_ex.items():
        bucket[ex] = (items, fake_now - age_s)


def _clear_cache():
    from backend.services import arbitrage_service
    arbitrage_service._cache.clear()


def teardown_function(_):
    _clear_cache()


# ── Flag off — no override ─────────────────────────────────────────────
def test_flag_off_no_override():
    ts = _reload_with_flag("0")
    _seed_cache([{"exchange": "binance", "symbol": "BTC", "price": 60000.0}])
    rows = [{
        "exchange": "binance", "symbol": "BTC", "side": "buy",
        "quantity": 0.1, "entry_price": 55000.0,
        "mark_price": 56000.0, "unrealized_pnl_usd": 100.0,
    }]
    ts._apply_local_upnl(rows)
    # Untouched (no mark_source even).
    assert rows[0]["mark_price"] == 56000.0
    assert rows[0]["unrealized_pnl_usd"] == 100.0
    assert "mark_source" not in rows[0]


# ── Flag on + fresh mark — long → green ────────────────────────────────
def test_flag_on_long_winning():
    ts = _reload_with_flag("1")
    _seed_cache([{"exchange": "binance", "symbol": "BTC", "price": 60000.0}])
    rows = [{
        "exchange": "binance", "symbol": "BTC", "side": "buy",
        "quantity": 0.1, "entry_price": 55000.0,
        "mark_price": 56000.0, "unrealized_pnl_usd": 100.0,
    }]
    ts._apply_local_upnl(rows)
    assert rows[0]["mark_price"] == 60000.0          # overridden
    # (60000 - 55000) * 0.1 * +1 = 500.0
    assert rows[0]["unrealized_pnl_usd"] == 500.0
    assert rows[0]["mark_source"] == "live"
    assert "mark_age_s" in rows[0]


# ── Flag on + fresh mark — short → green when price drops ──────────────
def test_flag_on_short_winning():
    ts = _reload_with_flag("1")
    _seed_cache([{"exchange": "bybit", "symbol": "ETH", "price": 2800.0}])
    rows = [{
        "exchange": "bybit", "symbol": "ETH", "side": "sell",
        "quantity": 2.0, "entry_price": 3000.0,
        "mark_price": 2950.0, "unrealized_pnl_usd": 100.0,
    }]
    ts._apply_local_upnl(rows)
    assert rows[0]["mark_price"] == 2800.0
    # (2800 - 3000) * 2.0 * -1 = +400.0  (short profits when price drops)
    assert rows[0]["unrealized_pnl_usd"] == 400.0
    assert rows[0]["mark_source"] == "live"


# ── Stale mark (>10s default) → fallback to venue ──────────────────────
def test_stale_mark_falls_back_to_venue():
    ts = _reload_with_flag("1")
    _seed_cache(
        [{"exchange": "okx", "symbol": "SOL", "price": 150.0}],
        age_s=120.0,  # 2 min — well past 10s threshold
    )
    rows = [{
        "exchange": "okx", "symbol": "SOL", "side": "buy",
        "quantity": 5.0, "entry_price": 140.0,
        "mark_price": 142.0, "unrealized_pnl_usd": 10.0,
    }]
    ts._apply_local_upnl(rows)
    # Mark + UPNL untouched.
    assert rows[0]["mark_price"] == 142.0
    assert rows[0]["unrealized_pnl_usd"] == 10.0
    assert rows[0]["mark_source"] == "venue"
    # mark_age_s present so the UI can show "X sec ago" badge.
    assert rows[0]["mark_age_s"] > 100


# ── No live mark for this symbol on this venue → fallback ──────────────
def test_no_live_mark_for_symbol():
    ts = _reload_with_flag("1")
    _seed_cache([{"exchange": "binance", "symbol": "BTC", "price": 60000.0}])
    rows = [{
        "exchange": "binance", "symbol": "DOGE", "side": "buy",
        "quantity": 1000.0, "entry_price": 0.10,
        "mark_price": 0.11, "unrealized_pnl_usd": 10.0,
    }]
    ts._apply_local_upnl(rows)
    assert rows[0]["mark_price"] == 0.11   # untouched
    assert rows[0]["mark_source"] == "venue"


# ── Skip rows with zero entry_price ────────────────────────────────────
def test_zero_entry_price_skipped():
    ts = _reload_with_flag("1")
    _seed_cache([{"exchange": "binance", "symbol": "BTC", "price": 60000.0}])
    rows = [{
        "exchange": "binance", "symbol": "BTC", "side": "buy",
        "quantity": 0.1, "entry_price": 0.0,
        "mark_price": 56000.0, "unrealized_pnl_usd": 100.0,
    }]
    ts._apply_local_upnl(rows)
    assert rows[0]["mark_price"] == 56000.0
    assert rows[0]["mark_source"] == "venue"
