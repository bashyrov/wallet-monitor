"""Phase 2 — server-side live position grouping into arb pairs.

Tests trade_service.group_live_positions, the server-side equivalent of
frontend's _acc_pair_positions. Same 12% tolerance + manual-first rule,
plus pair_mark_stale tagging from Phase 1.2.

Pure unit-tests on dict inputs — no DB, no FastAPI. Catches drift
between frontend and backend pair logic.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-32-chars-long-aaaa")


def _pos(ex, sym, side, qty, entry, mark, **kw):
    base = {
        "exchange": ex, "symbol": sym, "side": side,
        "quantity": qty, "entry_price": entry, "mark_price": mark,
    }
    base.update(kw)
    return base


# ── Two opposite-side legs on same symbol → auto-paired ────────────────
def test_auto_pair_long_short_same_size():
    from backend.services.trade_service import group_live_positions
    rows = [
        _pos("binance", "BTC", "buy",  0.10, 55000, 60000),
        _pos("bybit",   "BTC", "sell", 0.10, 56000, 60000),
    ]
    out = group_live_positions(rows)
    assert len(out["pairs"]) == 1
    assert len(out["singles"]) == 0
    p = out["pairs"][0]
    assert p["symbol"] == "BTC"
    assert p["long"]["exchange"] == "binance"
    assert p["short"]["exchange"] == "bybit"
    assert p["_manual"] is False
    assert p["mark_stale"] is False  # no mark_tick_ts on either → safe-default


# ── Notional skew >12% (and spread small) → NOT paired ─────────────────
def test_pair_rejected_when_notional_skew_above_tolerance():
    from backend.services.trade_service import group_live_positions
    # Long: 0.1 BTC × 60000 = $6000 notional
    # Short: 0.05 BTC × 60000 = $3000 notional
    # diff% = 50%, spread% ~0 — way over 12% tolerance.
    rows = [
        _pos("binance", "BTC", "buy",  0.10, 55000, 60000),
        _pos("bybit",   "BTC", "sell", 0.05, 55000, 60000),
    ]
    out = group_live_positions(rows)
    assert len(out["pairs"]) == 0
    assert len(out["singles"]) == 2


# ── Manual override beats notional check ───────────────────────────────
def test_manual_pair_bypasses_size_threshold():
    from backend.services.trade_service import group_live_positions
    rows = [
        # Way different notionals — auto would refuse.
        _pos("binance", "BTC", "buy",  0.10, 55000, 60000),
        _pos("bybit",   "BTC", "sell", 0.01, 55000, 60000),
    ]
    manual = [{"symbol": "BTC", "long_exchange": "binance", "short_exchange": "bybit"}]
    out = group_live_positions(rows, manual_pairs=manual)
    assert len(out["pairs"]) == 1
    assert out["pairs"][0]["_manual"] is True
    assert out["singles"] == []


# ── Two same-side positions → no pair ──────────────────────────────────
def test_two_longs_no_pair():
    from backend.services.trade_service import group_live_positions
    rows = [
        _pos("binance", "BTC", "buy", 0.10, 55000, 60000),
        _pos("bybit",   "BTC", "buy", 0.10, 56000, 60000),
    ]
    out = group_live_positions(rows)
    assert out["pairs"] == []
    assert len(out["singles"]) == 2


# ── Different symbols never pair ───────────────────────────────────────
def test_different_symbols_no_pair():
    from backend.services.trade_service import group_live_positions
    rows = [
        _pos("binance", "BTC", "buy",  0.10, 55000, 60000),
        _pos("bybit",   "ETH", "sell", 1.00, 2900, 3000),
    ]
    out = group_live_positions(rows)
    assert out["pairs"] == []
    assert len(out["singles"]) == 2


# ── Best-candidate-first when multiple pair candidates exist ───────────
def test_best_candidate_picked_first():
    from backend.services.trade_service import group_live_positions
    # Two longs, two shorts, all same symbol.
    # Long A (binance): qty 0.10 @ 55000  → notional 6000
    # Long B (okx):     qty 0.10 @ 55000  → notional 6000
    # Short A (bybit):  qty 0.10 @ 56000  → notional 6000  (matches both perfectly)
    # Short B (gate):   qty 0.20 @ 56000  → notional 11200 (~50% diff — won't pair with longs)
    rows = [
        _pos("binance", "BTC", "buy",  0.10, 55000, 60000),
        _pos("okx",     "BTC", "buy",  0.10, 55000, 60000),
        _pos("bybit",   "BTC", "sell", 0.10, 56000, 60000),
        _pos("gate",    "BTC", "sell", 0.20, 56000, 60000),
    ]
    out = group_live_positions(rows)
    assert len(out["pairs"]) == 1
    # gate short is the unpaired single.
    assert len(out["singles"]) == 2
    single_exs = sorted([s["exchange"] for s in out["singles"]])
    assert "gate" in single_exs


# ── Mark-stale tag from Phase 1.2 propagates to pair output ────────────
def test_pair_mark_stale_tag_propagates():
    from backend.services.trade_service import group_live_positions
    # Two legs with mark_tick_ts 10s apart — pair_mark_stale → True.
    rows = [
        _pos("binance", "BTC", "buy",  0.10, 55000, 60000, mark_tick_ts=1000.0),
        _pos("bybit",   "BTC", "sell", 0.10, 56000, 60000, mark_tick_ts=1010.0),
    ]
    out = group_live_positions(rows)
    assert len(out["pairs"]) == 1
    assert out["pairs"][0]["mark_stale"] is True


def test_pair_mark_in_sync_not_stale():
    from backend.services.trade_service import group_live_positions
    rows = [
        _pos("binance", "BTC", "buy",  0.10, 55000, 60000, mark_tick_ts=1000.0),
        _pos("bybit",   "BTC", "sell", 0.10, 56000, 60000, mark_tick_ts=1001.0),
    ]
    out = group_live_positions(rows)
    assert out["pairs"][0]["mark_stale"] is False


# ── Empty input → empty output ─────────────────────────────────────────
def test_empty_input():
    from backend.services.trade_service import group_live_positions
    out = group_live_positions([])
    assert out["pairs"] == []
    assert out["singles"] == []


# ── Manual pair missing one leg → falls through to singles ─────────────
def test_manual_pair_missing_leg_fallthrough():
    from backend.services.trade_service import group_live_positions
    rows = [_pos("binance", "BTC", "buy", 0.10, 55000, 60000)]
    manual = [{"symbol": "BTC", "long_exchange": "binance", "short_exchange": "bybit"}]
    out = group_live_positions(rows, manual_pairs=manual)
    assert out["pairs"] == []
    assert len(out["singles"]) == 1
