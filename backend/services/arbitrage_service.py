"""
Funding rate screener — fetches perpetual futures funding rates from multiple
exchanges using public (no-auth) endpoints. Per-exchange cache with 30s TTL.
"""
import asyncio
import logging
import time

import httpx

logger = logging.getLogger("avalant.screener")

_http = httpx.AsyncClient(
    timeout=10,
    headers={"User-Agent": "Mozilla/5.0"},
    follow_redirects=True,
)

# Per-exchange cache: {exchange: (rows, monotonic_fetched_at)}
_cache: dict[str, tuple[list, float]] = {}
CACHE_TTL = 30.0  # seconds


def _mono() -> float:
    return time.monotonic()


# ── Binance Futures ────────────────────────────────────────────────────────────
async def _fetch_binance() -> list[dict]:
    r = await _http.get("https://fapi.binance.com/fapi/v1/premiumIndex")
    r.raise_for_status()
    out = []
    for item in r.json():
        sym = item.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        token = sym[:-4]
        rate = float(item.get("lastFundingRate") or 0)
        next_ms = int(item.get("nextFundingTime") or 0)
        price = float(item.get("markPrice") or 0)
        if price == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "binance",
            "price": price,
            "rate": rate,           # per 8h
            "next_ts": next_ms // 1000,
            "interval_h": 8,
        })
    return out


# ── Bybit Linear ───────────────────────────────────────────────────────────────
async def _fetch_bybit() -> list[dict]:
    r = await _http.get("https://api.bybit.com/v5/market/tickers?category=linear")
    r.raise_for_status()
    out = []
    for item in r.json().get("result", {}).get("list", []):
        sym = item.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        token = sym[:-4]
        rate_str = item.get("fundingRate") or ""
        if not rate_str:
            continue
        rate = float(rate_str)
        next_ms = int(item.get("nextFundingTime") or 0)
        price = float(item.get("markPrice") or 0)
        if price == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "bybit",
            "price": price,
            "rate": rate,           # per funding interval (usually 8h)
            "next_ts": next_ms // 1000,
            "interval_h": 8,
        })
    return out


# ── Gate.io Futures (USDT-settled) ─────────────────────────────────────────────
async def _fetch_gate() -> list[dict]:
    r = await _http.get("https://api.gateio.ws/api/v4/futures/usdt/contracts")
    r.raise_for_status()
    out = []
    for item in r.json():
        name = item.get("name", "")   # e.g. "BTC_USDT"
        if not name.endswith("_USDT"):
            continue
        token = name[:-5]
        rate = float(item.get("funding_rate") or 0)
        next_ts = int(item.get("funding_next_apply") or 0)
        price = float(item.get("mark_price") or 0)
        if price == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "gate",
            "price": price,
            "rate": rate,
            "next_ts": next_ts,
            "interval_h": 8,
        })
    return out


# ── KuCoin Futures ─────────────────────────────────────────────────────────────
async def _fetch_kucoin() -> list[dict]:
    r = await _http.get("https://api-futures.kucoin.com/api/v1/contracts/active")
    r.raise_for_status()
    out = []
    for item in (r.json().get("data") or []):
        sym = item.get("symbol", "")   # e.g. "XBTUSDTM"
        if not sym.endswith("USDTM"):
            continue
        token = sym[:-5]               # strip "USDTM"
        if token == "XBT":
            token = "BTC"
        rate = float(item.get("fundingFeeRate") or 0)
        next_ms = int(item.get("nextFundingRateTime") or 0)
        price = float(item.get("indexPrice") or 0)
        if price == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "kucoin",
            "price": price,
            "rate": rate,
            "next_ts": next_ms // 1000,
            "interval_h": 8,
        })
    return out


# ── Hyperliquid (perp DEX) ─────────────────────────────────────────────────────
async def _fetch_hyperliquid() -> list[dict]:
    r = await _http.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "metaAndAssetCtxs"},
        headers={"Content-Type": "application/json"},
    )
    r.raise_for_status()
    meta, ctxs = r.json()
    universe = meta.get("universe", [])
    # Hyperliquid funds every hour; ctx.funding = hourly rate
    now_ts = int(time.time())
    next_ts = (now_ts // 3600 + 1) * 3600   # next full hour (UTC)
    out = []
    for asset_meta, ctx in zip(universe, ctxs):
        token = asset_meta.get("name", "")
        rate_1h = float(ctx.get("funding") or 0)
        price = float(ctx.get("markPx") or 0)
        if price == 0:
            continue
        out.append({
            "symbol": token,
            "exchange": "hyperliquid",
            "price": price,
            "rate": rate_1h,        # per 1h (interval_h = 1)
            "next_ts": next_ts,
            "interval_h": 1,
        })
    return out


# ── Dispatcher ─────────────────────────────────────────────────────────────────
FETCHERS: dict[str, object] = {
    "binance":     _fetch_binance,
    "bybit":       _fetch_bybit,
    "gate":        _fetch_gate,
    "kucoin":      _fetch_kucoin,
    "hyperliquid": _fetch_hyperliquid,
}


async def _get_rows(exchange: str) -> list[dict]:
    cached_rows, cached_at = _cache.get(exchange, ([], 0.0))
    if _mono() - cached_at < CACHE_TTL and cached_rows:
        return cached_rows
    try:
        rows = await FETCHERS[exchange]()
        _cache[exchange] = (rows, _mono())
        logger.debug("Screener %s: %d contracts", exchange, len(rows))
        return rows
    except Exception as exc:
        logger.warning("Screener %s fetch failed: %s", exchange, exc)
        return cached_rows   # serve stale on error


async def get_funding_data() -> dict:
    """
    Returns:
      {
        "ts": <unix seconds>,
        "exchanges": ["binance", ...],
        "rows": [
          {
            "symbol": "BTC",
            "exchange": "binance",
            "price": 103500.0,
            "rate": 0.0001,       # per interval
            "next_ts": 1234567890,
            "interval_h": 8,
            "apr": 10.95          # annualised %
          },
          ...
        ]
      }
    """
    results = await asyncio.gather(
        *(_get_rows(ex) for ex in FETCHERS),
        return_exceptions=True,
    )

    all_rows: list[dict] = []
    for ex, result in zip(FETCHERS.keys(), results):
        if isinstance(result, list):
            for row in result:
                # APR = rate * (8760 / interval_h) * 100
                row["apr"] = round(row["rate"] * (8760 / row["interval_h"]) * 100, 4)
            all_rows.extend(result)

    # Sort by |APR| descending — highest opportunity first
    all_rows.sort(key=lambda r: abs(r["apr"]), reverse=True)

    return {
        "ts": int(time.time()),
        "exchanges": list(FETCHERS.keys()),
        "rows": all_rows,
    }
