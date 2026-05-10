"""DEX-vs-Perp cash-and-carry arbitrage.

Pipeline:
  - Buy spot on an on-chain DEX (DexScreener = price source).
  - Short the perp on a CEX that lists the same ticker.
Matching:
  - CoinGecko maps symbol → canonical contract address (chosen by market-cap
    rank + preferred chain). We NEVER match by ticker: too many meme-tokens
    reuse CEX symbols.
  - DexScreener `/latest/dex/tokens/<address>` returns every pair for that
    exact token; we pick the highest-liquidity USD-or-wrapped-quote pair.
Earn:
  - short_funding_8h = -funding_rate × 8/interval (flip sign: we are short).
  - basis_pct = (perp − dex) / dex × 100 (positive = gap paid on entry).
Net = basis + short_funding − DEX round-trip − perp round-trip − slippage.

Runtime: on the fetcher container we run a **sync daemon thread** with its own
event-loop-free httpx.Client. That's the same trick funding_ws uses for its
REST backstop — the fetcher's asyncio loop is already saturated with 11 WS
adapters + compute, so doing DEX inside that loop starves the WS pings and
drops connections. Sync thread never yields to asyncio.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from . import arbitrage_service as _arb

logger = logging.getLogger("avalant.dex_arb")

# Sync client used by the daemon thread. Its own pool, never shared with the
# async arb / spot pools.
_sync_http = httpx.Client(
    timeout=httpx.Timeout(connect=4.0, read=6.0, write=4.0, pool=2.0),
    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    follow_redirects=True,
    limits=httpx.Limits(max_connections=16, max_keepalive_connections=8, keepalive_expiry=30),
    http2=False,
)

# Config. DexScreener's `/latest/dex/tokens/<csv>` endpoint caps responses
# at 30 pairs total regardless of how many addresses are requested — so
# multi-address batching gives us fewer pools per token, not more, and breaks
# the cross-pool consensus check. Single-address calls with a thread pool
# remain the right shape. Public rate limit is 300 req/min; 300 symbols on a
# 30s cycle = 600 req/min — we occasionally get rate-limited, handled via
# the flicker guard (retains last good snapshot).
DEX_REFRESH_INTERVAL = 30.0
_SYMBOL_BATCH_LIMIT = 900
_DEX_WORKERS = 12
MIN_DEX_LIQUIDITY_USD = 50_000.0
MIN_DEX_VOL_24H = 10_000.0
MAX_BASIS_PCT = 100.0      # only drop the most extreme collisions; rely on
                            # token_registry contract-address verify downstream
# Market-cap rank ceiling. Was 1_000 — too tight: CoinGecko has ~5-10k tokens
# with real DEX+CEX liquidity and mid-cap (rank 1000-5000) names routinely
# surface the most interesting basis opportunities. 5000 expands the candidate
# universe ~3-5× without flooding DexScreener (still bounded by _SYMBOL_BATCH_LIMIT).
MAX_MCAP_RANK = 5_000

# Native / wrapped tokens accepted as quote — DexScreener always returns
# priceUsd regardless of the quote, so restricting to USDC/USDT filters out
# most large-caps that trade WETH-quoted.
_ACCEPTED_QUOTES = {
    "USDC", "USDT", "DAI", "BUSD", "USDC.E", "FDUSD", "PYUSD",
    "WETH", "WBTC", "WBNB", "WSOL", "SOL", "ETH", "BNB", "MATIC", "WMATIC",
}

# Chain preference when a token is deployed on several — pick the venue
# that historically has the deepest DEX liquidity for that asset class.
_CHAIN_PREFERENCE = (
    "ethereum", "solana", "base", "arbitrum", "bsc", "polygon",
    "optimism", "avalanche", "blast", "linea", "scroll", "mantle", "sui", "ton",
)

# CoinGecko chain slug → DexScreener chainId
_CG_TO_DS = {
    "ethereum":       "ethereum",
    "solana":         "solana",
    "binance-smart-chain": "bsc",
    "polygon-pos":    "polygon",
    "arbitrum-one":   "arbitrum",
    "optimistic-ethereum": "optimism",
    "base":           "base",
    "avalanche":      "avalanche",
    "fantom":         "fantom",
    "linea":          "linea",
    "scroll":         "scroll",
    "mantle":         "mantle",
    "blast":          "blast",
    "zksync":         "zksync",
    "sui":            "sui",
    "tron":           "tron",
    "ton":            "ton",
    "aptos":          "aptos",
}


# ── CoinGecko resolver (cached 1h) ────────────────────────────────────────────
_CG_CACHE: dict[str, list[dict]] = {}
_CG_CACHE_TS: float = 0.0
_CG_TTL = 3600.0


def _refresh_cg_cache_sync() -> None:
    """Pull the full CoinGecko coin list + top-500 market ranks; build
    symbol → best-contract mapping. Called from the daemon thread."""
    global _CG_CACHE, _CG_CACHE_TS
    now = time.time()
    if _CG_CACHE and (now - _CG_CACHE_TS) < _CG_TTL:
        return
    try:
        r = _sync_http.get(
            "https://api.coingecko.com/api/v3/coins/list",
            params={"include_platform": "true"},
            timeout=30.0,
        )
        if r.status_code != 200:
            logger.warning("CoinGecko list: HTTP %s — keeping stale cache", r.status_code)
            return
        coins = r.json() or []

        rank_map: dict[str, int] = {}
        try:
            for page in (1, 2):
                m = _sync_http.get(
                    "https://api.coingecko.com/api/v3/coins/markets",
                    params={"vs_currency": "usd", "order": "market_cap_desc",
                            "per_page": 250, "page": page, "sparkline": "false"},
                    timeout=20.0,
                )
                if m.status_code == 200:
                    for row in (m.json() or []):
                        cid = row.get("id")
                        rk = row.get("market_cap_rank")
                        if cid and isinstance(rk, int):
                            rank_map[cid] = rk
        except Exception as e:
            logger.debug("CoinGecko markets rank: %s", e)

        new_cache: dict[str, list[dict]] = {}
        for c in coins:
            sym = (c.get("symbol") or "").upper()
            if not sym:
                continue
            platforms = c.get("platforms") or {}
            keep = {}
            for chain_cg, addr in platforms.items():
                if not addr:
                    continue
                ds = _CG_TO_DS.get(chain_cg)
                if not ds:
                    continue
                keep[ds] = addr
            if not keep:
                continue
            new_cache.setdefault(sym, []).append({
                "id": c.get("id"),
                "mcap_rank": rank_map.get(c.get("id") or "", 10_000),
                "platforms": keep,
            })
        for sym, entries in new_cache.items():
            entries.sort(key=lambda x: x["mcap_rank"])

        _CG_CACHE = new_cache
        _CG_CACHE_TS = time.time()
        logger.info("CoinGecko cache refreshed: %d symbols mapped", len(_CG_CACHE))
    except Exception as e:
        logger.warning("CoinGecko cache refresh failed: %s", e)


def _lookup_contract(symbol: str) -> tuple[str, str] | None:
    entries = _CG_CACHE.get(symbol.upper()) or []
    if not entries:
        return None
    platforms = entries[0]["platforms"]
    if not platforms:
        return None
    for pref in _CHAIN_PREFERENCE:
        if pref in platforms:
            return (pref, platforms[pref])
    chain, addr = next(iter(platforms.items()))
    return (chain, addr)


# Cross-pool sanity — if the top-liquidity pool's price diverges from the
# median of the top-N qualifying pools by more than this, the sample looks
# like a single-pool jitter tick (large swap in a thin pool, outlier quote)
# and we reject the cycle instead of emitting a phantom opp.
_POOL_CONSENSUS_MAX_DEV = 0.015   # 1.5% — well inside real arb noise, well outside tick-jitter
_POOL_CONSENSUS_MIN_POOLS = 2     # need at least 2 qualifying pools to vote

# Hysteresis — an opp has to survive at least one full refresh cycle before
# emission so that a single-cycle DexScreener tick can't surface a row. Set
# slightly below DEX_REFRESH_INTERVAL so it trips on the second-consecutive
# cycle rather than the third.
DEX_OPP_MIN_LIFETIME_S = 25.0
DEX_OPP_PURGE_AFTER_S = 300.0
_dex_opp_first_seen: dict[tuple[str, str], float] = {}
_dex_opp_last_seen:  dict[tuple[str, str], float] = {}


def _purge_stale_dex_opps(now_ts: float) -> None:
    """Keep the hysteresis dicts bounded — drop entries we haven't seen in
    a while. Safe to call from the daemon thread (single-threaded writer)."""
    dead = [k for k, ts in _dex_opp_last_seen.items() if now_ts - ts > DEX_OPP_PURGE_AFTER_S]
    for k in dead:
        _dex_opp_first_seen.pop(k, None)
        _dex_opp_last_seen.pop(k, None)


# ── DexScreener sync fetcher ──────────────────────────────────────────────────
def _pick_best_pool(pairs: list[dict], chain: str, addr_low: str) -> dict | None:
    """Pick the pool we'd emit for a single (chain, contract), applying
    the same cross-pool consensus guard as before. Shared between the
    single-address wrapper (kept for tests) and the batch routing path."""
    # Collect EVERY pool that matches (same chain, same contract, accepted
    # quote, >0 price) — not only ones over MIN_DEX_LIQUIDITY_USD. The small
    # pools are still useful as consensus voters for the cross-pool sanity
    # check; they just can't win `best` on their own.
    pools: list[dict] = []
    for p in pairs:
        if (p.get("chainId") or "") != chain:
            continue
        base = p.get("baseToken") or {}
        if (base.get("address") or "").lower() != addr_low:
            continue
        quote_sym = (p.get("quoteToken") or {}).get("symbol", "").upper()
        if quote_sym not in _ACCEPTED_QUOTES:
            continue
        try:
            liq_f = float((p.get("liquidity") or {}).get("usd") or 0)
            vol_f = float((p.get("volume") or {}).get("h24") or 0)
            price_f = float(p.get("priceUsd") or 0)
        except (TypeError, ValueError):
            continue
        if price_f <= 0:
            continue
        pools.append({
            "symbol":     base.get("symbol", "").upper(),
            "dex":        p.get("dexId") or "",
            "price":      price_f,
            "liq":        liq_f,
            "vol":        vol_f,
            "pair_addr":  p.get("pairAddress") or "",
            "url":        p.get("url") or "",
        })

    if not pools:
        return None

    eligible = [p for p in pools if p["liq"] >= MIN_DEX_LIQUIDITY_USD and p["vol"] >= MIN_DEX_VOL_24H]
    if not eligible:
        return None

    # Cross-pool consensus. Take the top-5 by liquidity (so dust pools don't
    # swing the median) and compute median price. Then pick `best` = the
    # highest-liquidity pool whose price is within _POOL_CONSENSUS_MAX_DEV
    # of the median — this way a single broken DexScreener quote (e.g. UNI's
    # $4.5M WETH-pair tick) doesn't disqualify the whole token, we just skip
    # past it to the next-best pool.
    voters = sorted(pools, key=lambda p: -p["liq"])[:5]
    if len(voters) >= _POOL_CONSENSUS_MIN_POOLS:
        prices = sorted(v["price"] for v in voters)
        median = prices[len(prices) // 2]
        if median <= 0:
            return None
        best = None
        for p in sorted(eligible, key=lambda p: -p["liq"]):
            if abs(p["price"] - median) / median <= _POOL_CONSENSUS_MAX_DEV:
                best = p
                break
        if best is None:
            # Every eligible pool disagrees with the median — genuine tick
            # jitter across the whole token; drop this cycle.
            logger.debug(
                "dex sanity drop: %s %s all %d eligible pools outside %.2f%% of median %.6f",
                chain, addr_low[:10], len(eligible), _POOL_CONSENSUS_MAX_DEV * 100, median,
            )
            return None
    else:
        # Only one qualifying pool — no consensus possible, accept.
        best = max(eligible, key=lambda p: p["liq"])

    return {
        "symbol":         best["symbol"],
        "chain":          chain,
        "dex":            best["dex"],
        "price":          best["price"],
        "liquidity_usd":  best["liq"],
        "volume_usd":     best["vol"],
        "pair_address":   best["pair_addr"],
        "url":            best["url"],
        "base_address":   addr_low,
    }


def _fetch_dex_by_contract_sync(chain: str, address: str) -> dict | None:
    """Single-address convenience wrapper. Kept for the existing test suite
    and any caller that only needs one lookup — the cycle path uses
    `_fetch_dex_batch_sync`."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
    try:
        r = _sync_http.get(url, timeout=4.0)
    except Exception as e:
        logger.debug("dex %s %s: %s", chain, address, e)
        return None
    if r.status_code != 200:
        return None
    try:
        pairs = (r.json() or {}).get("pairs") or []
    except Exception:
        return None
    return _pick_best_pool(pairs, chain, address.lower())


def _dex_fee_rt() -> float:
    return 0.6 + 0.2  # 0.3% swap × 2 + 0.2% slippage


# ── Perp-rows reader (sync, via shared arbitrage.json or in-memory cache) ────
def _read_perp_map_sync(min_vol_usd: float) -> dict[str, dict[str, dict]]:
    """Build {symbol: {exchange: row}} from the fetcher's in-memory _cache.
    Cross-thread dict reads are safe — we just snapshot what's there.
    Works on ANY process as long as `arbitrage_service._cache` has been
    populated (which it always is on the fetcher)."""
    perp_map: dict[str, dict[str, dict]] = {}
    for ex, (rows, _ts) in list(_arb._cache.items()):
        if ex == "lighter":
            continue
        if not rows:
            continue
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            perp_map.setdefault(sym, {})[ex] = r

    # Merge the WS rows too (funding_ws caches)
    try:
        from backend.services.funding_ws import get_ws_rows
        for ex in _arb.FETCHERS.keys():
            if ex == "lighter":
                continue
            ws_rows = get_ws_rows(ex) or []
            for r in ws_rows:
                sym = r.get("symbol")
                if not sym:
                    continue
                perp_map.setdefault(sym, {}).setdefault(ex, r)
    except Exception:
        pass
    return perp_map


def _build_opps_sync(dex_by_sym: dict[str, dict], perp_map: dict[str, dict[str, dict]],
                    min_perp_vol_usd: float) -> list[dict]:
    opps: list[dict] = []
    now_ts = time.time()
    # Purge stale hysteresis entries so the dicts don't grow unbounded.
    _purge_stale_dex_opps(now_ts)
    for sym, dex in dex_by_sym.items():
        perp_by_ex = perp_map.get(sym) or {}
        if not perp_by_ex or not dex:
            continue
        dex_price = dex["price"]
        for perp_ex, pdata in perp_by_ex.items():
            perp_price = pdata.get("price") or 0
            perp_vol = pdata.get("volume_usd") or 0
            raw_rate = pdata.get("rate")
            if perp_price <= 0 or raw_rate is None:
                continue
            try:
                rate_f = float(raw_rate)
            except (TypeError, ValueError):
                continue
            if rate_f == 0.0 or perp_vol < min_perp_vol_usd:
                continue
            interval_h = pdata.get("interval_h") or 8.0
            rate_8h = rate_f * (8.0 / interval_h) * 100
            short_funding = -rate_8h
            basis_pct = (perp_price - dex_price) / dex_price * 100
            if abs(basis_pct) > MAX_BASIS_PCT:
                continue
            gross = short_funding + basis_pct
            # No gross<=0 filter — show every spread. Hysteresis state is
            # updated per-cycle below regardless of sign.

            # Hysteresis: require the opp to survive at least one full
            # refresh cycle before emission. DexScreener single-pool ticks
            # die within 30s; real basis windows last minutes.
            key = (sym, perp_ex)
            first = _dex_opp_first_seen.get(key)
            if first is None:
                _dex_opp_first_seen[key] = now_ts
                _dex_opp_last_seen[key] = now_ts
                continue
            _dex_opp_last_seen[key] = now_ts
            if now_ts - first < DEX_OPP_MIN_LIFETIME_S:
                continue

            fee_dex_rt = _dex_fee_rt()
            fee_perp_rt = _arb._fee(perp_ex) * 100 * 2
            total_fees = fee_dex_rt + fee_perp_rt
            net = gross - total_fees
            # APR is funding-only (no entry-basis pickup) — sustainable
            # annual return. 8h window × 3 × 365 = 1095 ticks/year.
            funding_only = short_funding - total_fees
            net_apr = funding_only * (365.0 * 3) if funding_only > 0 else 0.0
            opps.append({
                "type": "dex_short",
                "symbol": sym,
                "dex_chain": dex["chain"],
                "dex_name": dex["dex"],
                "dex_pair_url": dex["url"],
                "dex_base_address": dex["base_address"],
                "short_exchange": perp_ex,
                "dex_price": dex_price,
                "perp_price": perp_price,
                "dex_liquidity_usd": dex["liquidity_usd"],
                "dex_volume_usd": dex["volume_usd"],
                "perp_volume_usd": perp_vol,
                "funding_rate": rate_f,
                "short_funding_8h": short_funding,
                "basis_pct": basis_pct,
                "gross": gross,
                "fee_dex": fee_dex_rt,
                "fee_perp": fee_perp_rt,
                "total_fees": total_fees,
                "net_profit": net,
                "net_apr": net_apr,
                "interval_h": interval_h,
                "next_ts": pdata.get("next_ts", 0),
            })
    opps.sort(key=lambda x: x["net_profit"], reverse=True)
    return opps[:200]


def _run_cycle_sync(min_perp_vol_usd: float = 10_000.0) -> dict:
    _refresh_cg_cache_sync()
    perp_map = _read_perp_map_sync(min_perp_vol_usd)
    if not perp_map:
        return {"opportunities": [], "generated_at": int(time.time()),
                "symbols_scanned": 0, "dex_hits": 0}

    def _best_perp_vol(sym: str) -> float:
        return max(
            (float(r.get("volume_usd") or 0) for r in perp_map[sym].values()),
            default=0.0,
        )

    # Select top-N by CoinGecko market cap (not perp volume). We want the
    # well-known market-cap leaders — they're liquid on both DEX and CEX and
    # avoid the long-tail memecoin noise.
    def _mcap_rank(sym: str) -> int:
        entries = _CG_CACHE.get(sym.upper()) or []
        return entries[0]["mcap_rank"] if entries else 10_000

    mappable = [s for s in perp_map if _lookup_contract(s)]
    # Sort by mcap rank ascending (lower = bigger); drop non-ranked (10_000)
    symbols = sorted(mappable, key=_mcap_rank)
    # Keep symbols with a real CG rank up to MAX_MCAP_RANK; batch limit caps
    # the per-cycle DexScreener fan-out.
    symbols = [s for s in symbols if _mcap_rank(s) < MAX_MCAP_RANK][:_SYMBOL_BATCH_LIMIT]

    # Parallel DexScreener fetch with a bounded thread pool. Each worker has
    # its own connection to _sync_http (httpx.Client is thread-safe) and its
    # own per-call timeout (4s). 12 workers × ~0.15s/call = ~15s for 300 syms.
    dex_by_sym: dict[str, dict] = {}
    def _one(sym: str) -> tuple[str, dict | None]:
        target = _lookup_contract(sym)
        if not target:
            return (sym, None)
        chain, addr = target
        try:
            return (sym, _fetch_dex_by_contract_sync(chain, addr))
        except Exception:
            return (sym, None)

    with ThreadPoolExecutor(max_workers=_DEX_WORKERS, thread_name_prefix="dex-fetch") as pool:
        for sym, dex in pool.map(_one, symbols):
            if dex:
                dex_by_sym[sym] = dex

    opps = _build_opps_sync(dex_by_sym, perp_map, min_perp_vol_usd)
    return {
        "opportunities": opps,
        "generated_at": int(time.time()),
        "symbols_scanned": len(symbols),
        "dex_hits": len(dex_by_sym),
    }


# ── API consumer (async, used by the FastAPI endpoint) ────────────────────────
async def get_dex_arbitrage_opportunities(min_vol_usd: float = 10_000.0) -> dict:
    """Web role reads the file cache; fetcher occasionally falls through here
    for a cold probe. Never runs the heavy sync cycle from an async request.

    Async read offloads the JSON parse to a thread — see notes in
    spot_arbitrage_service.get_spot_arbitrage_opportunities for why this
    matters under burst load."""
    cached = await _arb._read_file_cache_async("dex_arbitrage.json", max_age=120.0)
    if cached and isinstance(cached, dict) and cached.get("opportunities") is not None:
        return cached
    # Cold-start: wait up to 500 ms for the fetcher to land its first write
    # instead of flashing an empty table to the user.
    for _ in range(10):
        await asyncio.sleep(0.05)
        cached = await _arb._read_file_cache_async("dex_arbitrage.json", max_age=120.0)
        if cached and isinstance(cached, dict) and cached.get("opportunities") is not None:
            return cached
    return {"opportunities": [], "generated_at": int(time.time()),
            "symbols_scanned": 0, "dex_hits": 0, "cold": True}


# ── Daemon thread (sync, fetcher-side) ────────────────────────────────────────
_dex_thread: threading.Thread | None = None
_dex_stop = threading.Event()
_dex_lock_fd = None


def _worker_loop() -> None:
    logger.info("DEX worker thread running (interval=%.0fs)", DEX_REFRESH_INTERVAL)
    # Flicker guard — DexScreener periodically rate-limits us and a single
    # cycle returns 0 opps for ~30s before recovering. The UI would blink
    # empty for that window if we overwrote the file. Keep the previous
    # valid snapshot when the current cycle looks degraded (≥80% drop vs
    # last good OR empty while previous was non-empty). If stay-stale > 2m,
    # web readers time it out via max_age and we stop serving it anyway.
    last_good_count: int = 0
    last_write_ts: float = 0.0
    _FLICKER_WINDOW_S = 120.0  # abandon the stale-guard after this
    _MIN_RETAIN_RATIO = 0.20   # write new only if ≥20% of last good
    while not _dex_stop.is_set():
        t0 = time.time()
        try:
            result = _run_cycle_sync()
            current = len(result.get("opportunities") or [])
            now = time.time()
            too_thin = (
                last_good_count > 10
                and (now - last_write_ts) < _FLICKER_WINDOW_S
                and (current == 0 or current < last_good_count * _MIN_RETAIN_RATIO)
            )
            if too_thin:
                logger.info(
                    "dex refresh skipped (flicker guard): %d opps vs last_good=%d (%.1fs)",
                    current, last_good_count, time.time() - t0,
                )
            else:
                _arb._write_file_cache("dex_arbitrage.json", result)
                if current > 0:
                    last_good_count = current
                    last_write_ts = now
                logger.info(
                    "dex refresh: %d opps, %d/%d hits, %.1fs",
                    current,
                    result.get("dex_hits", 0),
                    result.get("symbols_scanned", 0),
                    time.time() - t0,
                )
        except Exception as exc:
            logger.warning("dex refresh failed: %s", exc)
        # Sleep in short chunks so stop-event is responsive
        remaining = max(1.0, DEX_REFRESH_INTERVAL - (time.time() - t0))
        _dex_stop.wait(remaining)


def start_dex_refresh_loop() -> None:
    """Start the DEX worker thread. File-lock guards double-start."""
    import fcntl
    global _dex_thread, _dex_lock_fd
    if _dex_thread and _dex_thread.is_alive():
        return
    try:
        _dex_lock_fd = open("/tmp/avalant_dex_refresh.lock", "w")
        fcntl.flock(_dex_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        logger.info("DEX refresh: another worker holds the lock — skipping")
        return
    _dex_stop.clear()
    _dex_thread = threading.Thread(target=_worker_loop, name="dex-refresh", daemon=True)
    _dex_thread.start()
    logger.info("DEX refresh thread started (every %.0fs)", DEX_REFRESH_INTERVAL)


def stop_dex_refresh_loop() -> None:
    global _dex_thread, _dex_lock_fd
    _dex_stop.set()
    if _dex_thread:
        _dex_thread.join(timeout=5.0)
    _dex_thread = None
    if _dex_lock_fd is not None:
        try:
            _dex_lock_fd.close()
        except Exception:
            pass
        _dex_lock_fd = None
