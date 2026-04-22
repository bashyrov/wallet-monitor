"""DEX-vs-Perp cash-and-carry arbitrage.

Logic:
  - Buy spot on an on-chain DEX (DexScreener as the price source).
  - Short the perp on a CEX that lists the same ticker.
Earn:
  - Funding on the short leg (flips sign: negative perp funding = shorts receive).
  - Basis (perp − dex) / dex. Positive = perp above spot, you capture the gap.
Cost:
  - DEX round-trip fees (swap + slippage, ~0.6% flat).
  - Perp taker round-trip fees.
  - Gas is position-size dependent, not modelled in net% — assume amortised.

Refresh: 30 s cadence. DexScreener free tier allows 300 reqs/min; we stay under
that by capping the symbol list + semaphore-limiting concurrency.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

from . import arbitrage_service as _arb

logger = logging.getLogger("avalant.dex_arb")

# Dedicated httpx client — DO NOT share with _arb._http or spot_arbitrage._http.
# DexScreener is slower and cross-pool traffic has already bitten us once.
_http = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=4.0, read=6.0, write=4.0, pool=2.0),
    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    follow_redirects=True,
    limits=httpx.Limits(max_connections=16, max_keepalive_connections=8, keepalive_expiry=30),
    http2=False,
)

# Per-call timeout — if DexScreener takes > this many seconds, skip and move on
_DEX_CALL_TIMEOUT = 2.5

# Native / wrapped tokens accepted as quote — DexScreener always returns
# priceUsd for the pair regardless of the quote, so restricting to USDC/USDT
# was a mistake (filtered out most large-cap tokens that trade WETH-quoted).
_ACCEPTED_QUOTES = {
    "USDC", "USDT", "DAI", "BUSD", "USDC.E", "FDUSD", "PYUSD",
    "WETH", "WBTC", "WBNB", "WSOL", "SOL", "ETH", "BNB", "MATIC", "WMATIC",
}

# Filters
MIN_DEX_LIQUIDITY_USD = 50_000.0   # skip illiquid long-tail memecoins
MIN_DEX_VOL_24H = 10_000.0         # some volume means real market
MAX_BASIS_PCT = 10.0               # sanity: prices more divergent than this
                                   #        almost always signal a problem

# Refresh cadence
DEX_REFRESH_INTERVAL = 30.0

# Cap how many CEX perp symbols we try to match each cycle
_SYMBOL_BATCH_LIMIT = 60

# DexScreener chain preference when a token is deployed on several — pick the
# venue that historically has the deepest DEX liquidity for that asset class.
_CHAIN_PREFERENCE = (
    "ethereum", "solana", "base", "arbitrum", "bsc", "polygon",
    "optimism", "avalanche", "blast", "linea", "scroll", "mantle", "sui", "ton",
)

# ── CoinGecko resolver: symbol → canonical contract ──────────────────────────
# We can't trust symbol matching — PEPE on Solana ≠ PEPE on Ethereum. CoinGecko
# publishes the canonical contract-per-chain for every token. We fetch it once
# an hour and use it to translate CEX tickers into DexScreener token addresses.
#
# Cache shape: {symbol_upper: [{id, mcap_rank, platforms: {chain: addr}}]}
_CG_CACHE: dict[str, list[dict]] = {}
_CG_CACHE_TS: float = 0.0
_CG_TTL = 3600.0
_CG_LOCK = asyncio.Lock()

# Map CoinGecko chain slug → DexScreener chainId. DexScreener uses slightly
# different ids for some L2s. Keep this list tight to major venues.
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


async def _ensure_cg_cache() -> None:
    """Refresh the symbol→contract map from CoinGecko every 1h."""
    global _CG_CACHE, _CG_CACHE_TS
    now = time.time()
    if _CG_CACHE and (now - _CG_CACHE_TS) < _CG_TTL:
        return
    async with _CG_LOCK:
        if _CG_CACHE and (time.time() - _CG_CACHE_TS) < _CG_TTL:
            return  # raced
        try:
            # 1) Full coin list with platforms (~20 MB, free, no auth)
            r = await _http.get(
                "https://api.coingecko.com/api/v3/coins/list",
                params={"include_platform": "true"},
                timeout=30.0,
            )
            if r.status_code != 200:
                logger.warning("CoinGecko list: HTTP %s — keeping stale cache", r.status_code)
                return
            coins = r.json() or []

            # 2) Top-500 by market cap — we rank collisions by mcap rank
            rank_map: dict[str, int] = {}
            try:
                for page in (1, 2):
                    m = await _http.get(
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
                # Filter to chains DexScreener actually covers
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
            # Sort each symbol's entries by mcap rank (lower = better)
            for sym, entries in new_cache.items():
                entries.sort(key=lambda x: x["mcap_rank"])

            _CG_CACHE = new_cache
            _CG_CACHE_TS = time.time()
            logger.info("CoinGecko cache refreshed: %d symbols mapped", len(_CG_CACHE))
        except Exception as e:
            logger.warning("CoinGecko cache refresh failed: %s", e)


def _lookup_contracts(symbol: str) -> list[tuple[str, str]]:
    """Return [(ds_chain, contract_address)] for a symbol — at most ONE entry,
    picked by CoinGecko market-cap rank and then by our chain preference list.
    Keeping this to a single pair per symbol is critical for staying inside
    DexScreener's rate budget (one cycle = one call per token)."""
    entries = _CG_CACHE.get(symbol.upper()) or []
    if not entries:
        return []
    platforms = entries[0]["platforms"]
    if not platforms:
        return []
    for pref in _CHAIN_PREFERENCE:
        if pref in platforms:
            return [(pref, platforms[pref])]
    # Nothing matched our preference list — take whatever is available
    chain, addr = next(iter(platforms.items()))
    return [(chain, addr)]


async def _fetch_dex_by_contract(chain: str, address: str) -> dict | None:
    """DexScreener pairs for a specific contract — no symbol ambiguity."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
    try:
        r = await asyncio.wait_for(_http.get(url), timeout=_DEX_CALL_TIMEOUT)
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug("dex by contract %s %s: %s", chain, address, e)
        return None
    if r.status_code != 200:
        return None
    try:
        pairs = (r.json() or {}).get("pairs") or []
    except Exception:
        return None

    best: dict | None = None
    best_liq = 0.0
    addr_low = address.lower()
    for p in pairs:
        if (p.get("chainId") or "") != chain:
            continue
        base = p.get("baseToken") or {}
        if (base.get("address") or "").lower() != addr_low:
            continue  # the token must be the base side
        quote_sym = (p.get("quoteToken") or {}).get("symbol", "").upper()
        if quote_sym not in _ACCEPTED_QUOTES:
            continue
        try:
            liq_f = float((p.get("liquidity") or {}).get("usd") or 0)
            vol_f = float((p.get("volume") or {}).get("h24") or 0)
            price_f = float(p.get("priceUsd") or 0)
        except (TypeError, ValueError):
            continue
        if price_f <= 0 or liq_f < MIN_DEX_LIQUIDITY_USD or vol_f < MIN_DEX_VOL_24H:
            continue
        if liq_f > best_liq:
            best_liq = liq_f
            best = {
                "symbol": base.get("symbol", "").upper(),
                "chain": chain,
                "dex": p.get("dexId") or "",
                "price": price_f,
                "liquidity_usd": liq_f,
                "volume_usd": vol_f,
                "pair_address": p.get("pairAddress") or "",
                "url": p.get("url") or "",
                "base_address": addr_low,
            }
    return best


async def _best_dex_match(symbol: str) -> dict | None:
    """Resolve symbol → single canonical contract (via CoinGecko), fetch it
    from DexScreener. Sequential — no gather / semaphore complexity to clash
    with the fetcher's event-loop load."""
    targets = _lookup_contracts(symbol)
    if not targets:
        return None
    chain, addr = targets[0]
    return await _fetch_dex_by_contract(chain, addr)


def _dex_fee_rt() -> float:
    """Conservative DEX round-trip cost: 0.3% swap × 2 + 0.2% slippage."""
    return 0.6 + 0.2  # in %


async def get_dex_arbitrage_opportunities(min_perp_vol_usd: float = 100_000.0) -> dict:
    """DEX-spot vs CEX-perp cash-and-carry.

    Web role reads from the shared spot_arbitrage.json counterpart
    (dex_arbitrage.json) written by the fetcher every DEX_REFRESH_INTERVAL s.
    """
    if os.environ.get("AVALANT_ROLE", "").lower() == "web":
        cached = _arb._read_file_cache("dex_arbitrage.json", max_age=120.0)
        if cached and isinstance(cached, dict) and cached.get("opportunities") is not None:
            return cached
        # cache miss on first request — return empty rather than run the gather
        return {"opportunities": [], "generated_at": int(time.time()), "cold": True}

    # Fetcher path: build perp map first, pick top-volume tickers, query DexScreener.
    await _ensure_cg_cache()

    perp_exs = [ex for ex in _arb.FETCHERS.keys() if ex != "lighter"]
    perp_results = await asyncio.gather(
        *[_arb._get_rows(ex) for ex in perp_exs],
        return_exceptions=True,
    )
    # {symbol: {ex: row}}
    perp_map: dict[str, dict[str, dict]] = {}
    for ex, rows in zip(perp_exs, perp_results):
        if not isinstance(rows, list):
            continue
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            perp_map.setdefault(sym, {})[ex] = r

    # Filter out symbols with no CoinGecko contract mapping — we can't match
    # by address without one, and matching by symbol alone is unsafe.
    # Then pick top by perp volume.
    def _best_perp_vol(sym: str) -> float:
        return max(
            (float(r.get("volume_usd") or 0) for r in perp_map[sym].values()),
            default=0.0,
        )

    mappable = [s for s in perp_map if _lookup_contracts(s)]
    symbols = sorted(mappable, key=_best_perp_vol, reverse=True)[:_SYMBOL_BATCH_LIMIT]

    # Sequential scan — each call has its own 2.5s timeout. Event-loop-friendly
    # because we yield after every call and never hold a semaphore slot across
    # gather tasks that compete with busy WS handlers.
    dex_results: list[dict | None] = []
    for sym in symbols:
        try:
            dex_results.append(await _best_dex_match(sym))
        except Exception as e:
            logger.debug("dex match %s: %s", sym, e)
            dex_results.append(None)

    opps: list[dict] = []
    for sym, dex in zip(symbols, dex_results):
        if not dex:
            continue
        dex_price = dex["price"]
        perp_by_ex = perp_map.get(sym) or {}
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
                continue  # likely a ticker collision (same symbol, different token)
            gross = short_funding + basis_pct
            if gross <= 0:
                continue
            fee_dex_rt = _dex_fee_rt()
            fee_perp_rt = _arb._fee(perp_ex) * 100 * 2
            total_fees = fee_dex_rt + fee_perp_rt
            net = gross - total_fees
            net_apr = net * (365.0 * 3) if net > 0 else 0.0
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
    return {
        "opportunities": opps[:200],
        "generated_at": int(time.time()),
        "symbols_scanned": len(symbols),
        "dex_hits": sum(1 for d in dex_results if d),
    }


# ── Background refresh loop (fetcher-side) ────────────────────────────────────
_dex_refresh_task: asyncio.Task | None = None
_dex_refresh_lock_fd = None


async def _dex_refresh_loop() -> None:
    in_flight = False
    while True:
        if not in_flight:
            in_flight = True
            t0 = time.time()
            try:
                result = await get_dex_arbitrage_opportunities()
                _arb._write_file_cache("dex_arbitrage.json", result)
                dt = time.time() - t0
                logger.info(
                    "dex refresh: %d opps, %d/%d hits, %.1fs",
                    len(result.get("opportunities") or []),
                    result.get("dex_hits", 0),
                    result.get("symbols_scanned", 0),
                    dt,
                )
            except Exception as exc:
                logger.warning("dex refresh: %s", exc)
            finally:
                in_flight = False
        await asyncio.sleep(DEX_REFRESH_INTERVAL)


def start_dex_refresh_loop() -> None:
    import fcntl
    global _dex_refresh_task, _dex_refresh_lock_fd
    if _dex_refresh_task and not _dex_refresh_task.done():
        return
    try:
        _dex_refresh_lock_fd = open("/tmp/avalant_dex_refresh.lock", "w")
        fcntl.flock(_dex_refresh_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        logger.info("DEX refresh: another worker holds the lock — skipping")
        return
    _dex_refresh_task = asyncio.create_task(_dex_refresh_loop())
    logger.info("DEX refresh loop started (every %.0fs)", DEX_REFRESH_INTERVAL)


def stop_dex_refresh_loop() -> None:
    global _dex_refresh_task, _dex_refresh_lock_fd
    if _dex_refresh_task and not _dex_refresh_task.done():
        _dex_refresh_task.cancel()
    _dex_refresh_task = None
    if _dex_refresh_lock_fd is not None:
        try:
            _dex_refresh_lock_fd.close()
        except Exception:
            pass
        _dex_refresh_lock_fd = None
