"""Spot-vs-Perp cash-and-carry arbitrage.

Logic:
  - Buy spot on exchange A (spot market, no leverage/funding).
  - Short perp on exchange B (perpetual futures, pays/receives funding).
Earn:
  - Short funding flips sign: if perp funding is NEGATIVE, shorts RECEIVE payments.
  - Basis: (perp_price - spot_price) / spot_price. Positive = perp trades above spot.
Cost:
  - Spot taker fee round-trip (open + close).
  - Perp taker fee round-trip.

Freshness: 6s cache per exchange, matching the futures cache.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

from . import arbitrage_service as _arb

logger = logging.getLogger("avalant.spot_arb")

# Dedicated client — shares no pool with the futures arb loop so concurrent
# refreshes don't starve each other.
_http = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0),
    headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip, deflate"},
    follow_redirects=True,
    limits=httpx.Limits(max_connections=64, max_keepalive_connections=16, keepalive_expiry=30),
    http2=False,
)

_spot_cache: dict[str, tuple[list[dict], float]] = {}
SPOT_CACHE_TTL = 6.0


# ── Spot fee config (taker, as fraction) ──────────────────────────────────────
SPOT_FEES: dict[str, float] = {
    "binance":  0.001,
    "bybit":    0.001,
    "okx":      0.001,
    "gate":     0.001,
    "kucoin":   0.001,
    "mexc":     0.0,
    "bitget":   0.001,
    "bingx":    0.001,
}
_DEFAULT_SPOT_FEE = 0.001


def _spot_fee(exchange: str) -> float:
    return SPOT_FEES.get(exchange, _DEFAULT_SPOT_FEE)


# ── Per-exchange spot fetchers ────────────────────────────────────────────────
async def _fetch_binance_spot() -> list[dict]:
    r = await _http.get("https://api.binance.com/api/v3/ticker/24hr")
    if r.status_code != 200:
        return []
    out: list[dict] = []
    for x in r.json():
        s = x.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        try:
            price = float(x.get("lastPrice") or 0)
            vol = float(x.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-4], "price": price, "volume_usd": vol})
    return out


async def _fetch_bybit_spot() -> list[dict]:
    r = await _http.get("https://api.bybit.com/v5/market/tickers", params={"category": "spot"})
    if r.status_code != 200:
        return []
    j = r.json()
    out: list[dict] = []
    for x in (j.get("result", {}).get("list") or []):
        s = x.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        try:
            price = float(x.get("lastPrice") or 0)
            vol = float(x.get("turnover24h") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-4], "price": price, "volume_usd": vol})
    return out


async def _fetch_okx_spot() -> list[dict]:
    r = await _http.get("https://www.okx.com/api/v5/market/tickers", params={"instType": "SPOT"})
    if r.status_code != 200:
        return []
    j = r.json()
    out: list[dict] = []
    for x in (j.get("data") or []):
        s = x.get("instId", "")  # "BTC-USDT"
        if not s.endswith("-USDT"):
            continue
        try:
            price = float(x.get("last") or 0)
            vol_ccy = float(x.get("volCcy24h") or 0)  # quoted in base ccy — convert via price
        except (TypeError, ValueError):
            continue
        vol = vol_ccy * price  # volCcy24h is in base asset; multiply by price → USDT
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-5], "price": price, "volume_usd": vol})
    return out


async def _fetch_gate_spot() -> list[dict]:
    r = await _http.get("https://api.gateio.ws/api/v4/spot/tickers")
    if r.status_code != 200:
        return []
    out: list[dict] = []
    for x in r.json():
        s = x.get("currency_pair", "")
        if not s.endswith("_USDT"):
            continue
        try:
            price = float(x.get("last") or 0)
            vol = float(x.get("quote_volume") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-5], "price": price, "volume_usd": vol})
    return out


async def _fetch_kucoin_spot() -> list[dict]:
    r = await _http.get("https://api.kucoin.com/api/v1/market/allTickers")
    if r.status_code != 200:
        return []
    j = r.json()
    out: list[dict] = []
    for x in (j.get("data", {}).get("ticker") or []):
        s = x.get("symbol", "")
        if not s.endswith("-USDT"):
            continue
        try:
            price = float(x.get("last") or 0)
            vol = float(x.get("volValue") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-5], "price": price, "volume_usd": vol})
    return out


async def _fetch_mexc_spot() -> list[dict]:
    r = await _http.get("https://api.mexc.com/api/v3/ticker/24hr")
    if r.status_code != 200:
        return []
    out: list[dict] = []
    for x in r.json():
        s = x.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        try:
            price = float(x.get("lastPrice") or 0)
            vol = float(x.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-4], "price": price, "volume_usd": vol})
    return out


async def _fetch_bitget_spot() -> list[dict]:
    r = await _http.get("https://api.bitget.com/api/v2/spot/market/tickers")
    if r.status_code != 200:
        return []
    j = r.json()
    out: list[dict] = []
    for x in (j.get("data") or []):
        s = x.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        try:
            price = float(x.get("lastPr") or 0)
            vol = float(x.get("usdtVolume") or x.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-4], "price": price, "volume_usd": vol})
    return out


async def _fetch_bingx_spot() -> list[dict]:
    r = await _http.get("https://open-api.bingx.com/openApi/spot/v1/ticker/24hr")
    if r.status_code != 200:
        return []
    j = r.json()
    out: list[dict] = []
    for x in (j.get("data") or []):
        s = x.get("symbol", "")
        if not s.endswith("-USDT"):
            continue
        try:
            price = float(x.get("lastPrice") or 0)
            vol = float(x.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and vol > 0:
            out.append({"symbol": s[:-5], "price": price, "volume_usd": vol})
    return out


SPOT_FETCHERS = {
    "binance": _fetch_binance_spot,
    "bybit":   _fetch_bybit_spot,
    "okx":     _fetch_okx_spot,
    "gate":    _fetch_gate_spot,
    "kucoin":  _fetch_kucoin_spot,
    "mexc":    _fetch_mexc_spot,
    "bitget":  _fetch_bitget_spot,
    "bingx":   _fetch_bingx_spot,
}

SPOT_EXCHANGES = list(SPOT_FETCHERS.keys())


async def get_spot_rows(exchange: str) -> list[dict]:
    """Cached per-exchange spot tickers."""
    now = _arb._mono()
    cached = _spot_cache.get(exchange)
    if cached and (now - cached[1]) < SPOT_CACHE_TTL:
        return cached[0]
    fn = SPOT_FETCHERS.get(exchange)
    if not fn:
        return []
    try:
        rows = await asyncio.wait_for(fn(), timeout=15.0)
    except Exception as e:
        logger.warning("spot fetch %s failed: %s", exchange, type(e).__name__)
        rows = cached[0] if cached else []
    _spot_cache[exchange] = (rows, now)
    return rows


async def get_spot_arbitrage_opportunities(min_vol_usd: float = 100_000.0) -> dict:
    """Cross-exchange spot-short cash-and-carry opportunities.

    Returns rows with positive gross (basis + inverted funding > 0) only,
    sorted by net profit descending.

    Web role reads from the shared file cache that the fetcher writes every
    2 s — same pattern as the futures arbitrage feed.
    """
    if os.environ.get("AVALANT_ROLE", "").lower() == "web":
        # Web NEVER computes — always serves whatever the fetcher wrote.
        # Use a generous max_age so a fetcher hiccup doesn't produce a
        # 10-second page-load while the web worker tries to recompute.
        cached = _arb._read_file_cache("spot_arbitrage.json", max_age=120.0)
        if cached and isinstance(cached, dict):
            return cached
        return {"opportunities": [], "generated_at": int(time.time()), "spot_exchanges": SPOT_EXCHANGES}

    # Fetch spot tickers for every supported spot venue in parallel
    spot_results = await asyncio.gather(
        *[get_spot_rows(ex) for ex in SPOT_EXCHANGES],
        return_exceptions=True,
    )
    spot_map: dict[str, dict[str, dict]] = {}
    for ex, rows in zip(SPOT_EXCHANGES, spot_results):
        if not isinstance(rows, list):
            continue
        for r in rows:
            sym = r["symbol"]
            spot_map.setdefault(sym, {})[ex] = r

    # Pull perp rows from the same source the futures arbitrage uses so we stay
    # in lock-step on freshness.
    perp_exs = [ex for ex in _arb.FETCHERS.keys() if ex != "lighter"]
    perp_results = await asyncio.gather(
        *[_arb._get_rows(ex) for ex in perp_exs],
        return_exceptions=True,
    )
    perp_map: dict[str, dict[str, dict]] = {}
    for ex, rows in zip(perp_exs, perp_results):
        if not isinstance(rows, list):
            continue
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            perp_map.setdefault(sym, {})[ex] = r

    opps: list[dict] = []
    for sym, spot_by_ex in spot_map.items():
        perp_by_ex = perp_map.get(sym)
        if not perp_by_ex:
            continue
        for spot_ex, sdata in spot_by_ex.items():
            spot_price = sdata.get("price") or 0
            spot_vol = sdata.get("volume_usd") or 0
            if spot_price <= 0 or spot_vol < min_vol_usd:
                continue
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
                if rate_f == 0.0 or perp_vol < min_vol_usd:
                    continue
                interval_h = pdata.get("interval_h") or 8.0
                # Normalize to 8h window
                rate_8h = rate_f * (8.0 / interval_h) * 100  # percent
                # Short perp → we pay if funding>0, receive if funding<0
                short_funding = -rate_8h
                basis_pct = (perp_price - spot_price) / spot_price * 100
                # Sanity filter: |basis| > 5% means the "same" ticker is almost
                # certainly a different token (e.g. MEXC "META" is Metaverse,
                # KuCoin "META" is Meta Pool). Real cash-and-carry basis for
                # a live USDT pair never exceeds a couple of percent.
                if abs(basis_pct) > 5.0:
                    continue
                gross = short_funding + basis_pct
                fee_spot_rt = _spot_fee(spot_ex) * 100 * 2  # round-trip, %
                fee_perp_rt = _arb._fee(perp_ex) * 100 * 2
                total_fees = fee_spot_rt + fee_perp_rt
                net = gross - total_fees
                # Annualized: 8h window repeats 3 × 365 = 1095 times/year
                net_apr = net * (365.0 * 3) if net > 0 else 0.0
                if gross <= 0:
                    continue
                opps.append({
                    "type": "spot_short",
                    "symbol": sym,
                    "spot_exchange": spot_ex,
                    "short_exchange": perp_ex,
                    "spot_price": spot_price,
                    "perp_price": perp_price,
                    "spot_volume_usd": spot_vol,
                    "perp_volume_usd": perp_vol,
                    "funding_rate": rate_f,
                    "short_funding_8h": short_funding,
                    "basis_pct": basis_pct,
                    "gross": gross,
                    "fee_spot": fee_spot_rt,
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
        "spot_exchanges": SPOT_EXCHANGES,
    }


# ── Background refresh loop (fetcher-side) ────────────────────────────────────
SPOT_REFRESH_INTERVAL = 2.0  # s — match the futures REST backstop cadence

_spot_refresh_task: asyncio.Task | None = None
_spot_refresh_lock_fd = None


async def _spot_refresh_loop() -> None:
    """Recompute spot-short arb opportunities every SPOT_REFRESH_INTERVAL
    and write spot_arbitrage.json. Skips a cycle if the previous compute
    is still in flight."""
    in_flight = False
    while True:
        if not in_flight:
            in_flight = True
            try:
                result = await get_spot_arbitrage_opportunities()
                _arb._write_file_cache("spot_arbitrage.json", result)
            except Exception as exc:
                logger.warning("spot refresh: %s", exc)
            finally:
                in_flight = False
        await asyncio.sleep(SPOT_REFRESH_INTERVAL)


def start_spot_refresh_loop() -> None:
    """Start the spot refresh loop. File-lock ensures only one process runs
    it. Safe to call from any number of workers."""
    import fcntl
    global _spot_refresh_task, _spot_refresh_lock_fd
    if _spot_refresh_task and not _spot_refresh_task.done():
        return
    try:
        _spot_refresh_lock_fd = open("/tmp/avalant_spot_refresh.lock", "w")
        fcntl.flock(_spot_refresh_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        logger.info("Spot refresh: another worker holds the lock — skipping")
        return
    _spot_refresh_task = asyncio.create_task(_spot_refresh_loop())
    logger.info("Spot refresh loop started")


def stop_spot_refresh_loop() -> None:
    global _spot_refresh_task, _spot_refresh_lock_fd
    if _spot_refresh_task and not _spot_refresh_task.done():
        _spot_refresh_task.cancel()
    _spot_refresh_task = None
    if _spot_refresh_lock_fd is not None:
        try:
            _spot_refresh_lock_fd.close()
        except Exception:
            pass
        _spot_refresh_lock_fd = None
