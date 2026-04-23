"""DEX-arb phantom guards: pool-consensus sanity + hysteresis."""
from __future__ import annotations

import time
from unittest.mock import patch

from backend.services import dex_arbitrage_service as dx


def _mock_dexscreener(pairs):
    class R:
        status_code = 200
        def json(self): return {"pairs": pairs}
    class Client:
        def get(self, *a, **kw): return R()
    return Client()


def _pair(chain, addr, price, liq, vol, quote="USDC", dex="uniswap"):
    return {
        "chainId": chain,
        "dexId": dex,
        "baseToken": {"address": addr, "symbol": "FOO"},
        "quoteToken": {"symbol": quote},
        "priceUsd": str(price),
        "liquidity": {"usd": liq},
        "volume": {"h24": vol},
        "pairAddress": f"{addr}-{price}",
        "url": f"https://example.com/{price}",
    }


def test_pool_consensus_falls_back_when_top_pool_outlier():
    """Top-liq pool jumps 8% above the other pools (classic jitter / broken
    quote, e.g. UNI's $4.5M WETH-pair). Should emit the NEXT-best pool
    whose price agrees with the median, not drop the whole token."""
    addr = "0xabc"
    pairs = [
        _pair("ethereum", addr, 1.08, 500_000, 200_000),   # outlier top-liq
        _pair("ethereum", addr, 1.00, 300_000, 150_000),   # consensus, next-best
        _pair("ethereum", addr, 1.005, 200_000, 100_000),
        _pair("ethereum", addr, 0.99,  150_000, 90_000),
    ]
    with patch.object(dx, "_sync_http", _mock_dexscreener(pairs)):
        out = dx._fetch_dex_by_contract_sync("ethereum", addr)
    assert out is not None
    # We skipped the $1.08 outlier and picked the $1.00 pool.
    assert abs(out["price"] - 1.00) < 1e-9
    assert out["liquidity_usd"] == 300_000


def test_pool_consensus_drops_when_every_eligible_pool_disagrees():
    """Voter pool population (including small, ineligible pools) anchors
    the median at $1.00, but every pool above the liquidity threshold is
    ~15-20% off. Nothing safe to emit — drop this cycle."""
    addr = "0xabc"
    pairs = [
        # Small voter pools below liq threshold (50K) — anchor the median
        _pair("ethereum", addr, 1.00, 30_000, 5_000),
        _pair("ethereum", addr, 1.00, 30_000, 5_000),
        _pair("ethereum", addr, 1.00, 30_000, 5_000),
        # Eligible pools all outliers vs the $1.00 consensus
        _pair("ethereum", addr, 1.20, 500_000, 200_000),
        _pair("ethereum", addr, 1.15, 300_000, 150_000),
    ]
    with patch.object(dx, "_sync_http", _mock_dexscreener(pairs)):
        out = dx._fetch_dex_by_contract_sync("ethereum", addr)
    assert out is None


def test_pool_consensus_accepts_tight_cluster():
    """All pools within 0.3% of each other — healthy price, emit."""
    addr = "0xdef"
    pairs = [
        _pair("ethereum", addr, 1.002, 500_000, 200_000),
        _pair("ethereum", addr, 1.000, 300_000, 150_000),
        _pair("ethereum", addr, 1.003, 200_000, 100_000),
    ]
    with patch.object(dx, "_sync_http", _mock_dexscreener(pairs)):
        out = dx._fetch_dex_by_contract_sync("ethereum", addr)
    assert out is not None
    assert abs(out["price"] - 1.002) < 1e-9


def test_pool_consensus_single_pool_passes():
    """Only one qualifying pool — can't vote, so accept (no consensus check)."""
    addr = "0xeee"
    pairs = [_pair("ethereum", addr, 1.00, 300_000, 100_000)]
    with patch.object(dx, "_sync_http", _mock_dexscreener(pairs)):
        out = dx._fetch_dex_by_contract_sync("ethereum", addr)
    assert out is not None


def test_hysteresis_skips_first_cycle():
    """First time an opp is seen, it must NOT be emitted. Second cycle
    (after the min-lifetime window) emits it."""
    dx._dex_opp_first_seen.clear()
    dx._dex_opp_last_seen.clear()
    dex = {"SOL": {"price": 100.0, "chain": "solana", "dex": "raydium",
                   "liquidity_usd": 1e6, "volume_usd": 1e6, "url": "", "base_address": ""}}
    # rate=-0.001 → short_funding +0.1%, basis +3% → gross +3.1%
    perp = {"SOL": {"binance": {"price": 103.0, "volume_usd": 5_000_000,
                                 "rate": -0.001, "interval_h": 8.0}}}

    # Cycle 1 — new opp → first_seen stamped, skipped
    opps1 = dx._build_opps_sync(dex, perp, min_perp_vol_usd=100_000)
    assert len(opps1) == 0

    # Backdate first_seen past the min-lifetime window (simulate cycle 2)
    key = ("SOL", "binance")
    assert key in dx._dex_opp_first_seen
    dx._dex_opp_first_seen[key] = time.time() - (dx.DEX_OPP_MIN_LIFETIME_S + 5.0)

    # Cycle 2 — opp persisted past window → emit
    opps2 = dx._build_opps_sync(dex, perp, min_perp_vol_usd=100_000)
    assert len(opps2) == 1
    assert opps2[0]["symbol"] == "SOL"
    assert opps2[0]["short_exchange"] == "binance"


def test_hysteresis_resets_when_opp_vanishes():
    """If gross flips negative, drop state so the re-appearance has to re-qualify."""
    dx._dex_opp_first_seen.clear()
    dx._dex_opp_last_seen.clear()
    dex = {"SOL": {"price": 100.0, "chain": "solana", "dex": "raydium",
                   "liquidity_usd": 1e6, "volume_usd": 1e6, "url": "", "base_address": ""}}
    # Cycle 1 — gross > 0, stamps first_seen
    perp_good = {"SOL": {"binance": {"price": 103.0, "volume_usd": 5_000_000,
                                      "rate": -0.001, "interval_h": 8.0}}}
    dx._build_opps_sync(dex, perp_good, 100_000)
    assert ("SOL", "binance") in dx._dex_opp_first_seen

    # Cycle 2 — gross < 0 (perp below dex, positive funding), state should clear
    perp_bad = {"SOL": {"binance": {"price": 97.0, "volume_usd": 5_000_000,
                                     "rate": 0.001, "interval_h": 8.0}}}
    dx._build_opps_sync(dex, perp_bad, 100_000)
    assert ("SOL", "binance") not in dx._dex_opp_first_seen
