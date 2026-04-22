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
    timeout=httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=3.0),
    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    follow_redirects=True,
    limits=httpx.Limits(max_connections=32, max_keepalive_connections=16, keepalive_expiry=30),
    http2=False,
)

# DexScreener free-tier rate limit: 300 rpm = 5 rps. Semaphore keeps us under.
_DEX_SEM = asyncio.Semaphore(5)

# USD-like quote tokens — pairs quoted in these are directly comparable to CEX USDT price.
_USD_QUOTES = {"USDC", "USDT", "DAI", "BUSD", "USDC.E", "FDUSD", "PYUSD"}

# Filters
MIN_DEX_LIQUIDITY_USD = 50_000.0   # skip illiquid long-tail memecoins
MIN_DEX_VOL_24H = 10_000.0         # some volume means real market
MAX_BASIS_PCT = 10.0               # sanity: drop rows with wildly divergent prices
                                   #        (usually a different token with the same ticker)

# Refresh cadence
DEX_REFRESH_INTERVAL = 30.0

# How many top-volume perp symbols we feed DexScreener per cycle (keeps us
# comfortably under the rate limit — 200 symbols @ 5 rps = ~40s for a batch,
# so we set refresh to 30s and let the gather overlap for the tail).
_SYMBOL_BATCH_LIMIT = 200


async def _search_dex_for_symbol(symbol: str) -> dict | None:
    """Return the highest-liquidity USD-quoted DEX pair for a ticker, or None."""
    url = f"https://api.dexscreener.com/latest/dex/search?q={symbol}"
    async with _DEX_SEM:
        try:
            r = await _http.get(url)
        except Exception as e:
            logger.debug("dex search %s failed: %s", symbol, e)
            return None
    if r.status_code != 200:
        return None
    try:
        pairs = (r.json() or {}).get("pairs") or []
    except Exception:
        return None

    best: dict | None = None
    best_liq = 0.0
    sym_u = symbol.upper()
    for p in pairs:
        base_sym = (p.get("baseToken") or {}).get("symbol", "").upper()
        quote_sym = (p.get("quoteToken") or {}).get("symbol", "").upper()
        if base_sym != sym_u:
            continue
        if quote_sym not in _USD_QUOTES:
            continue
        liq = (p.get("liquidity") or {}).get("usd") or 0
        vol = (p.get("volume") or {}).get("h24") or 0
        price = p.get("priceUsd")
        if not price:
            continue
        try:
            liq_f = float(liq)
            vol_f = float(vol)
            price_f = float(price)
        except (TypeError, ValueError):
            continue
        if liq_f < MIN_DEX_LIQUIDITY_USD or vol_f < MIN_DEX_VOL_24H:
            continue
        if price_f <= 0:
            continue
        if liq_f > best_liq:
            best_liq = liq_f
            best = {
                "symbol": sym_u,
                "chain": p.get("chainId") or "",
                "dex": p.get("dexId") or "",
                "price": price_f,
                "liquidity_usd": liq_f,
                "volume_usd": vol_f,
                "pair_address": p.get("pairAddress") or "",
                "url": p.get("url") or "",
                "base_address": (p.get("baseToken") or {}).get("address") or "",
            }
    return best


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

    # Pick top symbols by best perp volume (any exchange)
    def _best_perp_vol(sym: str) -> float:
        return max(
            (float(r.get("volume_usd") or 0) for r in perp_map[sym].values()),
            default=0.0,
        )

    symbols = sorted(perp_map.keys(), key=_best_perp_vol, reverse=True)[:_SYMBOL_BATCH_LIMIT]

    # Query DexScreener in parallel (semaphore-limited)
    dex_results = await asyncio.gather(*[_search_dex_for_symbol(s) for s in symbols])

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
