import asyncio
import json
import logging

# orjson is a C-backed JSON serialiser — ~5× faster than stdlib on our
# typical payloads (arb diff ~200 rows, funding diff ~500 rows). Critical
# for the broadcast hot path: at 0.3s BROADCAST_INTERVAL × N WS clients,
# every ms we shave off the encode is multiplicative. Fallback keeps the
# code working in dev envs without orjson installed.
try:
    import orjson as _orjson
    def _fast_dumps(obj) -> str:
        return _orjson.dumps(obj).decode()
except ImportError:  # pragma: no cover
    def _fast_dumps(obj) -> str:
        return json.dumps(obj, separators=(",", ":"))
import os as _os
import time

import httpx
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect


def _env_float(name: str, default: float) -> float:
    """Read a float env var with a fallback. Used for tunable cadence knobs
    (refresh / broadcast intervals) so prod can tweak without rebuild."""
    try:
        v = _os.environ.get(name)
        return float(v) if v else default
    except (TypeError, ValueError):
        return default

from backend.api.deps import get_current_user
from backend.services.arbitrage_service import (
    get_arbitrage_opportunities, get_funding_data, _get_interval_map, _IVL_FETCHERS, _cache,
    EXCHANGE_FEES, _http as _arb_http,
)
from backend.services.auth_service import decode_token

router = APIRouter(prefix="/screener", tags=["screener"])
logger = logging.getLogger("avalant.screener")

# ── TTL caches for hot /arb page endpoints ─────────────────────────────────────
# key → (value, ts). `time() - ts < TTL` means cache hit.
_ob_cache: dict[tuple[str, str, int], tuple[dict, float]] = {}
_ph_cache: dict[tuple[str, str, str], tuple[dict, float]] = {}   # (symbol, long, short) → arb-price-history
_fh_cache: dict[tuple[str, str, str], tuple[dict, float]] = {}   # (symbol, long, short) → arb-history
_oi_cache: dict[tuple[str, str, str], tuple[dict, float]] = {}   # (symbol, long, short) → open-interest

_OB_TTL  = 0.5      # orderbook: 500ms
_PH_TTL  = 30.0     # price-history (1h candles): 30s
_FH_TTL  = 30.0     # funding-history: 30s
_OI_TTL  = 60.0     # open-interest: 60s

# ── REST endpoints ─────────────────────────────────────────────────────────────

@router.get("/funding")
async def funding_rates():
    """Funding rates across perpetual futures exchanges. Cached 30s per exchange."""
    return await get_funding_data()


@router.get("/long-short")
async def long_short_opportunities():
    """Cross-exchange funding arbitrage (perp-long vs perp-short) with price spread + fees.
    Canonical endpoint for the Long/Short mode."""
    return await get_arbitrage_opportunities()


@router.get("/arbitrage", deprecated=True)
async def arbitrage_opportunities():
    """Legacy alias for /long-short. Kept so existing bookmarks / API clients
    don't 404 after the rename. Remove once frontend callers are migrated."""
    return await get_arbitrage_opportunities()


@router.get("/spot-short")
async def spot_short_opportunities():
    """Spot-short cash-and-carry: buy spot on one venue, short perp on another.
    Canonical endpoint."""
    from backend.services.spot_arbitrage_service import get_spot_arbitrage_opportunities
    return await get_spot_arbitrage_opportunities()


@router.get("/spot-arbitrage", deprecated=True)
async def spot_arbitrage_opportunities():
    """Legacy alias for /spot-short."""
    from backend.services.spot_arbitrage_service import get_spot_arbitrage_opportunities
    return await get_spot_arbitrage_opportunities()


@router.get("/dex-short")
async def dex_short_opportunities():
    """DEX-short cash-and-carry: buy spot on a DEX (via DexScreener), short perp on CEX.
    Canonical endpoint."""
    from backend.services.dex_arbitrage_service import get_dex_arbitrage_opportunities
    return await get_dex_arbitrage_opportunities()


@router.get("/dex-arbitrage", deprecated=True)
async def dex_arbitrage_opportunities():
    """Legacy alias for /dex-short."""
    from backend.services.dex_arbitrage_service import get_dex_arbitrage_opportunities
    return await get_dex_arbitrage_opportunities()


@router.get("/all-arbitrage")
async def all_arbitrage():
    """Combined futures-arb + spot-short arb + dex-short arb, sorted by net profit."""
    from backend.services.spot_arbitrage_service import get_spot_arbitrage_opportunities as _spot
    from backend.services.dex_arbitrage_service import get_dex_arbitrage_opportunities as _dex
    fut, spot, dex = await asyncio.gather(
        get_arbitrage_opportunities(),
        _spot(),
        _dex(),
        return_exceptions=True,
    )
    fut_opps  = [] if isinstance(fut,  BaseException) else (fut.get("opportunities")  or [])
    spot_opps = [] if isinstance(spot, BaseException) else (spot.get("opportunities") or [])
    dex_opps  = [] if isinstance(dex,  BaseException) else (dex.get("opportunities")  or [])
    for r in fut_opps:
        r.setdefault("type", "futures")
    merged = list(fut_opps) + list(spot_opps) + list(dex_opps)
    merged.sort(key=lambda x: x.get("net_profit", 0.0), reverse=True)
    return {
        "opportunities": merged[:500],
        "generated_at": int(time.time()),
        "counts": {
            "futures": len(fut_opps),
            "spot_short": len(spot_opps),
            "dex_short": len(dex_opps),
        },
    }


_availability_cache: dict = {"data": None, "ts": 0.0}
_AVAILABILITY_TTL = 10.0


@router.get("/exchange-health")
async def exchange_health():
    """Per-exchange freshness snapshot for the UI status dots.

    Payload: {exchanges: {binance: {age_s, healthy, via, ...}, ...},
              generated_at: unix_ts}

    Readers render a green dot for `healthy`, amber for age > freshness
    threshold but < stale threshold, red for unhealthy / missing. Arb
    rows whose exchanges are unhealthy get dimmed in the UI.
    """
    from backend.services.arbitrage_service import get_exchange_health
    return {"exchanges": get_exchange_health(), "generated_at": time.time()}


@router.get("/availability")
async def availability():
    """Tiny payload: enabled exchanges + all current funding symbols. Used by
    the /arb pre-flight to confirm the selected exchange/symbol aren't admin-
    disabled. Served from the shared funding.json file cache so every
    worker returns in <30ms without any upstream refetch."""
    from backend.services import admin_settings
    from backend.services.arbitrage_service import _read_file_cache

    now = time.time()
    if _availability_cache["data"] and now - _availability_cache["ts"] < _AVAILABILITY_TTL:
        return _availability_cache["data"]

    # Shared file cache — written by the broadcaster every 3s. Tolerate up
    # to 30s stale; the user-visible cost of a slightly stale symbol list is
    # zero (we just show the page).
    data = _read_file_cache("funding.json", max_age=30.0)
    if not data:
        # Brand-new container / cold start — fall back to the expensive path
        data = await get_funding_data()

    result = {
        "exchanges": data.get("exchanges", []),
        "symbols": sorted({r["symbol"] for r in data.get("rows", [])}),
        "hidden_symbols": sorted(admin_settings.get_hidden_symbols()),
    }
    _availability_cache["data"] = result
    _availability_cache["ts"] = now
    return result


@router.get("/pair")
async def pair_opp(
    symbol: str = Query(..., pattern=r"^[A-Za-z0-9]{1,16}$"),
    long_ex: str = Query(..., min_length=2, max_length=24),
    short_ex: str = Query(..., min_length=2, max_length=24),
):
    """Lightweight per-pair arb data. Fetches only the 2 needed exchanges
    instead of all 12 — returns the same _opp shape the /arb page needs.
    Falls back to cached full arb result if warm."""
    sym = symbol.upper()
    long_ex = long_ex.lower()
    short_ex = short_ex.lower()

    # Try cached full arb result first (free if < 10s old)
    from backend.services.arbitrage_service import _arb_result_cache, _ARB_CACHE_TTL
    import time as _t
    if _arb_result_cache["data"] and _t.time() - _arb_result_cache["ts"] < _ARB_CACHE_TTL:
        for o in _arb_result_cache["data"].get("opportunities", []):
            if o["symbol"] == sym and o["long_exchange"] == long_ex and o["short_exchange"] == short_ex:
                return {"source": "cache", "opp": o}

    # Cache miss — fetch only the 2 exchanges via get_cached_rates (read from _cache, zero HTTP)
    from backend.services.arbitrage_service import get_cached_rates, EXCHANGE_FEES
    rates = get_cached_rates()
    r_long  = rates.get(f"{long_ex}:{sym}")
    r_short = rates.get(f"{short_ex}:{sym}")
    if r_long and r_short:
        rate_l = r_long["rate"] * (8.0 / r_long.get("interval_h", 8))
        rate_s = r_short["rate"] * (8.0 / r_short.get("interval_h", 8))
        gross = rate_s - rate_l
        fee_l = EXCHANGE_FEES.get(long_ex, 0.0005)
        fee_s = EXCHANGE_FEES.get(short_ex, 0.0005)
        total_fees = 2.0 * (fee_l + fee_s)
        p_l = r_long.get("price", 0)
        p_s = r_short.get("price", 0)
        spread = (p_s - p_l) / p_l if p_l > 0 else 0.0
        net = gross + spread - total_fees
        return {"source": "rates", "opp": {
            "symbol": sym, "long_exchange": long_ex, "short_exchange": short_ex,
            "long_rate": round(rate_l * 100, 6), "short_rate": round(rate_s * 100, 6),
            "long_price": p_l, "short_price": p_s,
            "long_volume": 0, "short_volume": 0,
            "long_interval_h": r_long.get("interval_h", 8),
            "short_interval_h": r_short.get("interval_h", 8),
            "gross_funding": round(gross * 100, 6),
            "price_spread": round(spread * 100, 4),
            "fee_long": round(fee_l * 100, 4), "fee_short": round(fee_s * 100, 4),
            "total_fees": round(total_fees * 100, 4),
            "net_profit": round(net * 100, 6),
            "gross_apr": round(gross * (8760/8) * 100, 4),
            "net_apr": round(net * (8760/8) * 100, 4),
            "valid_price": p_l <= p_s,
        }}

    return {"source": "empty", "opp": None}


# ── Funding history per exchange/symbol ────────────────────────────────────────

async def _fetch_history_for(exchange: str, symbol: str, limit: int = 90) -> list[dict]:
    """Fetch historical funding rates for a symbol on a given exchange."""
    try:
        c = _arb_http  # reuse persistent client with keepalive
        if True:
            if exchange == "binance":
                sym = symbol + "USDT"
                r = await c.get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}&limit={limit}")
                return [{"ts": int(x["fundingTime"]) // 1000, "rate": float(x["fundingRate"])} for x in r.json()]

            elif exchange == "bybit":
                sym = symbol + "USDT"
                r = await c.get(f"https://api.bybit.com/v5/market/funding/history?category=linear&symbol={sym}&limit={limit}")
                items = r.json().get("result", {}).get("list", [])
                return [{"ts": int(x["fundingRateTimestamp"]) // 1000, "rate": float(x["fundingRate"])} for x in reversed(items)]

            elif exchange == "okx":
                inst = symbol + "-USDT-SWAP"
                r = await c.get(f"https://www.okx.com/api/v5/public/funding-rate-history?instId={inst}&limit={limit}")
                items = r.json().get("data", [])
                return [{"ts": int(x["fundingTime"]) // 1000, "rate": float(x["fundingRate"])} for x in reversed(items)]

            elif exchange == "gate":
                contract = symbol + "_USDT"
                r = await c.get(f"https://api.gateio.ws/api/v4/futures/usdt/funding_rate?contract={contract}&limit={limit}")
                return [{"ts": int(x["t"]), "rate": float(x["r"])} for x in r.json()]

            elif exchange == "kucoin":
                sym = symbol + "USDTM"
                if symbol == "BTC":
                    sym = "XBTUSDTM"
                to_ts = int(time.time() * 1000)
                from_ts = to_ts - limit * 8 * 3600 * 1000
                r = await c.get(f"https://api-futures.kucoin.com/api/v1/contract/funding-rates?symbol={sym}&from={from_ts}&to={to_ts}")
                items = r.json().get("data", [])
                if isinstance(items, dict):
                    items = items.get("dataList", [])
                return sorted([{"ts": int(x["timepoint"]) // 1000, "rate": float(x["fundingRate"])} for x in items], key=lambda x: x["ts"])

            elif exchange == "mexc":
                sym = symbol + "_USDT"
                r = await c.get(f"https://contract.mexc.com/api/v1/contract/funding_rate/history?symbol={sym}&page_size={limit}&page_num=1")
                items = (r.json().get("data") or {}).get("resultList") or []
                return [{"ts": int(x["settleTime"]) // 1000, "rate": float(x["fundingRate"])} for x in reversed(items)]

            elif exchange == "bitget":
                sym = symbol + "USDT"
                r = await c.get(f"https://api.bitget.com/api/v2/mix/market/history-fund-rate?symbol={sym}&productType=USDT-FUTURES&pageSize={limit}")
                items = r.json().get("data", [])
                return [{"ts": int(x["fundingTime"]) // 1000, "rate": float(x["fundingRate"])} for x in reversed(items)]

            elif exchange == "aster":
                sym = symbol + "USDT"
                r = await c.get(f"https://fapi.asterdex.com/fapi/v1/fundingRate?symbol={sym}&limit={limit}")
                return [{"ts": int(x["fundingTime"]) // 1000, "rate": float(x["fundingRate"])} for x in r.json()]

            elif exchange == "hyperliquid":
                now_ms = int(time.time() * 1000)
                start_ms = now_ms - limit * 3600 * 1000
                r = await c.post("https://api.hyperliquid.xyz/info",
                    json={"type": "fundingHistory", "coin": symbol, "startTime": start_ms},
                    headers={"Content-Type": "application/json"})
                return [{"ts": int(x["time"]) // 1000, "rate": float(x["fundingRate"])} for x in r.json()]

            elif exchange == "bingx":
                sym = symbol + "-USDT"
                r = await c.get(f"https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate?symbol={sym}&limit={limit}")
                raw = r.json().get("data") or []
                items = raw.get("fundingRateList") if isinstance(raw, dict) else raw
                return sorted([{"ts": int(x["fundingTime"]) // 1000, "rate": float(x["fundingRate"])} for x in (items or [])], key=lambda x: x["ts"])

    except Exception as exc:
        logger.warning("History %s/%s failed: %s", exchange, symbol, exc)
    return []


async def _fetch_price_history(exchange: str, symbol: str, limit: int = 100) -> list[dict]:
    """Fetch OHLCV 1h candles → list of {ts, open, high, low, close}."""
    try:
        c = _arb_http
        if True:
            if exchange == "binance":
                sym = symbol + "USDT"
                r = await c.get(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1h&limit={limit}")
                return [{"ts": int(x[0])//1000, "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in r.json()]

            elif exchange == "bybit":
                sym = symbol + "USDT"
                r = await c.get(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}&interval=60&limit={limit}")
                items = r.json().get("result", {}).get("list", [])
                return sorted([{"ts": int(x[0])//1000, "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in items], key=lambda x: x["ts"])

            elif exchange == "okx":
                inst = symbol + "-USDT-SWAP"
                r = await c.get(f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar=1H&limit={limit}")
                items = r.json().get("data", [])
                return sorted([{"ts": int(x[0])//1000, "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in items], key=lambda x: x["ts"])

            elif exchange == "gate":
                r = await c.get(f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract={symbol}_USDT&interval=1h&limit={limit}")
                return [{"ts": int(x["t"]), "o": float(x["o"]), "h": float(x["h"]), "l": float(x["l"]), "c": float(x["c"])} for x in r.json()]

            elif exchange == "kucoin":
                sym = symbol + "USDTM"
                if symbol == "BTC": sym = "XBTUSDTM"
                to_ms = int(time.time() * 1000)
                from_ms = to_ms - limit * 3600 * 1000
                r = await c.get(f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={sym}&granularity=60&from={from_ms}&to={to_ms}")
                items = r.json().get("data", [])
                return [{"ts": int(x[0])//1000, "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in items]

            elif exchange == "mexc":
                r = await c.get(f"https://contract.mexc.com/api/v1/contract/kline/{symbol}_USDT?interval=Min60&limit={limit}")
                d = r.json().get("data", {})
                ts_list = d.get("time", [])
                opens  = d.get("open",  [])
                highs  = d.get("high",  [])
                lows   = d.get("low",   [])
                closes = d.get("close", [])
                return [{"ts": int(ts_list[i]), "o": float(opens[i]), "h": float(highs[i]), "l": float(lows[i]), "c": float(closes[i])} for i in range(len(ts_list))]

            elif exchange == "bitget":
                sym = symbol + "USDT"
                r = await c.get(f"https://api.bitget.com/api/v2/mix/market/candles?symbol={sym}&productType=USDT-FUTURES&granularity=1H&limit={limit}")
                items = r.json().get("data", [])
                return sorted([{"ts": int(x[0])//1000, "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in items], key=lambda x: x["ts"])

            elif exchange == "aster":
                sym = symbol + "USDT"
                r = await c.get(f"https://fapi.asterdex.com/fapi/v1/klines?symbol={sym}&interval=1h&limit={limit}")
                return [{"ts": int(x[0])//1000, "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])} for x in r.json()]

            elif exchange == "bingx":
                sym = symbol + "-USDT"
                r = await c.get(f"https://open-api.bingx.com/openApi/swap/v3/quote/klines?symbol={sym}&interval=1h&limit={limit}")
                items = r.json().get("data", [])
                return sorted([{"ts": int(x["time"])//1000, "o": float(x["open"]), "h": float(x["high"]), "l": float(x["low"]), "c": float(x["close"])} for x in items], key=lambda x: x["ts"])

    except Exception as exc:
        logger.warning("Price history %s/%s failed: %s", exchange, symbol, exc)
    return []


_ORDERBOOK_EX = {
    "binance","bybit","okx","gate","kucoin","mexc","bitget",
    "aster","hyperliquid","bingx","whitebit",
}

_SPOT_OB_EX = {"binance","bybit","okx","gate","kucoin","mexc","bitget","bingx"}


@router.get("/orderbook-spot")
async def get_spot_orderbook(
    symbol: str = Query(..., pattern=r"^[A-Za-z0-9]{1,16}$"),
    exchange: str = Query(..., min_length=2, max_length=24),
    limit: int = Query(20, ge=1, le=100),
):
    """Best-ask/best-bid snapshot from a CEX **spot** market — for the Spot /
    Short detail terminal. Uses the venue's public REST (no WS cache yet).
    Returns the same shape as /orderbook: {bids: [[px, qty], ...], asks: [...]}."""
    from fastapi import HTTPException
    import httpx as _httpx
    ex = exchange.lower()
    sym = symbol.upper()
    if ex not in _SPOT_OB_EX:
        raise HTTPException(400, f"unsupported exchange for spot orderbook: {ex}")

    urls = {
        "binance": (f"https://api.binance.com/api/v3/depth?symbol={sym}USDT&limit={min(limit, 100)}", "binance"),
        "bybit":   (f"https://api.bybit.com/v5/market/orderbook?category=spot&symbol={sym}USDT&limit={min(limit, 50)}", "bybit"),
        "okx":     (f"https://www.okx.com/api/v5/market/books?instId={sym}-USDT&sz={min(limit, 50)}", "okx"),
        "gate":    (f"https://api.gateio.ws/api/v4/spot/order_book?currency_pair={sym}_USDT&limit={min(limit, 100)}", "gate"),
        "kucoin":  (f"https://api.kucoin.com/api/v1/market/orderbook/level2_20?symbol={sym}-USDT", "kucoin"),
        "mexc":    (f"https://api.mexc.com/api/v3/depth?symbol={sym}USDT&limit={min(limit, 100)}", "mexc"),
        "bitget":  (f"https://api.bitget.com/api/v2/spot/market/orderbook?symbol={sym}USDT&limit={min(limit, 100)}", "bitget"),
        "bingx":   (f"https://open-api.bingx.com/openApi/spot/v1/market/depth?symbol={sym}-USDT&limit={min(limit, 100)}", "bingx"),
    }
    url, kind = urls[ex]
    try:
        async with _httpx.AsyncClient(timeout=6.0, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(url)
        if r.status_code != 200:
            return {"bids": [], "asks": []}
        j = r.json() or {}
    except Exception:
        return {"bids": [], "asks": []}

    def _norm(arr):
        out = []
        for x in arr or []:
            try:
                out.append([float(x[0]), float(x[1])])
            except (TypeError, ValueError, IndexError):
                continue
        return out

    if kind == "binance" or kind == "mexc":
        return {"bids": _norm(j.get("bids")), "asks": _norm(j.get("asks"))}
    if kind == "bybit":
        d = (j.get("result") or {})
        return {"bids": _norm(d.get("b")), "asks": _norm(d.get("a"))}
    if kind == "okx":
        d = ((j.get("data") or [{}])[0])
        return {"bids": _norm(d.get("bids")), "asks": _norm(d.get("asks"))}
    if kind == "gate":
        return {"bids": _norm(j.get("bids")), "asks": _norm(j.get("asks"))}
    if kind == "kucoin":
        d = j.get("data") or {}
        return {"bids": _norm(d.get("bids")), "asks": _norm(d.get("asks"))}
    if kind == "bitget":
        d = (j.get("data") or {})
        return {"bids": _norm(d.get("bids")), "asks": _norm(d.get("asks"))}
    if kind == "bingx":
        d = (j.get("data") or {})
        return {"bids": _norm(d.get("bids")), "asks": _norm(d.get("asks"))}
    return {"bids": [], "asks": []}


@router.get("/orderbook")
async def get_orderbook(
    symbol: str = Query(..., pattern=r"^[A-Za-z0-9]{1,16}$"),
    exchange: str = Query(..., min_length=2, max_length=24),
    limit: int = Query(200, ge=1, le=500),
):
    from fastapi import HTTPException
    from backend.services.orderbook_cache import get_cached_orderbook

    exchange = exchange.lower()
    symbol = symbol.upper()
    if exchange not in _ORDERBOOK_EX:
        raise HTTPException(400, f"unsupported exchange for orderbook: {exchange}")
    return await get_cached_orderbook(exchange, symbol, limit)


@router.get("/orderbooks")
async def get_orderbooks(
    pairs: str = Query(..., min_length=3, max_length=256,
                       description="Comma-separated exchange:SYMBOL pairs, e.g. binance:BTC,okx:BTC"),
    limit: int = Query(20, ge=1, le=100),
):
    """Batch variant of /orderbook — fetches every (exchange, symbol) pair in
    parallel and returns them as one map. Saves a round-trip when the /arb
    page polls both legs at 150ms intervals."""
    from backend.services.orderbook_cache import get_cached_orderbook
    import asyncio as _asyncio

    items: list[tuple[str, str]] = []
    for raw in pairs.split(",")[:8]:  # cap at 8 to keep the endpoint cheap
        raw = raw.strip()
        if not raw or ":" not in raw:
            continue
        ex, sym = raw.split(":", 1)
        ex = ex.strip().lower()
        sym = sym.strip().upper()
        if not ex or not sym or ex not in _ORDERBOOK_EX:
            continue
        items.append((ex, sym))

    if not items:
        return {}

    results = await _asyncio.gather(
        *(get_cached_orderbook(ex, sym, limit) for ex, sym in items),
        return_exceptions=True,
    )
    out: dict[str, dict] = {}
    for (ex, sym), r in zip(items, results):
        out[f"{ex}:{sym}"] = r if isinstance(r, dict) else {"bids": [], "asks": []}
    return out


@router.get("/arb-price-history")
async def arb_price_history(
    symbol: str = Query(...),
    long_ex: str = Query(...),
    short_ex: str = Query(...),
):
    key = (symbol, long_ex, short_ex)
    hit = _ph_cache.get(key)
    if hit and time.time() - hit[1] < _PH_TTL:
        return hit[0]
    long_prices, short_prices = await asyncio.gather(
        _fetch_price_history(long_ex, symbol),
        _fetch_price_history(short_ex, symbol),
    )
    out = {
        "symbol": symbol,
        "long_exchange": long_ex,
        "short_exchange": short_ex,
        "long_prices": long_prices,
        "short_prices": short_prices,
    }
    _ph_cache[key] = (out, time.time())
    return out


@router.get("/all-exchanges-funding")
async def all_exchanges_funding(
    symbol: str = Query(...),
):
    """Current funding rate for a symbol across all exchanges that list it."""
    data = await get_funding_data()
    sym_upper = symbol.upper()
    rows = [r for r in data["rows"] if r["symbol"] == sym_upper]
    # Sort by rate descending
    rows.sort(key=lambda r: r["rate"], reverse=True)
    return {"symbol": sym_upper, "ts": data["ts"], "rates": rows}


async def _fetch_open_interest(exchange: str, symbol: str) -> dict | None:
    """Fetch open interest for a symbol on a given exchange."""
    try:
        c = _arb_http
        if True:
            if exchange == "binance":
                r = await c.get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}USDT")
                d = r.json()
                return {"exchange": exchange, "oi": float(d.get("openInterest", 0)), "unit": "contracts"}
            elif exchange == "bybit":
                r = await c.get(f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={symbol}USDT&intervalTime=5min&limit=1")
                items = r.json().get("result", {}).get("list", [])
                oi = float(items[0].get("openInterest", 0)) if items else 0
                return {"exchange": exchange, "oi": oi, "unit": "contracts"}
            elif exchange == "okx":
                r = await c.get(f"https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history?instId={symbol}-USDT-SWAP&period=5m&limit=1")
                items = r.json().get("data", [])
                oi = float(items[0][1]) if items else 0
                return {"exchange": exchange, "oi": oi, "unit": "contracts"}
            elif exchange == "gate":
                r = await c.get(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{symbol}_USDT")
                d = r.json()
                return {"exchange": exchange, "oi": float(d.get("total_size", 0)), "unit": "contracts"}
            elif exchange == "hyperliquid":
                r = await c.post("https://api.hyperliquid.xyz/info",
                    json={"type": "metaAndAssetCtxs"},
                    headers={"Content-Type": "application/json"})
                data = r.json()
                if isinstance(data, list) and len(data) >= 2:
                    universe = data[0].get("universe", [])
                    ctxs = data[1]
                    for i, asset in enumerate(universe):
                        if asset.get("name") == symbol and i < len(ctxs):
                            oi = float(ctxs[i].get("openInterest", 0))
                            return {"exchange": exchange, "oi": oi, "unit": "contracts"}
            elif exchange == "aster":
                r = await c.get(f"https://fapi.asterdex.com/fapi/v1/openInterest?symbol={symbol}USDT")
                d = r.json()
                return {"exchange": exchange, "oi": float(d.get("openInterest", 0)), "unit": "contracts"}
            elif exchange == "bingx":
                r = await c.get(f"https://open-api.bingx.com/openApi/swap/v2/quote/openInterest?symbol={symbol}-USDT")
                d = r.json().get("data", {})
                return {"exchange": exchange, "oi": float(d.get("openInterest", 0)), "unit": "contracts"}
            elif exchange == "mexc":
                r = await c.get(f"https://contract.mexc.com/api/v1/contract/ticker?symbol={symbol}_USDT")
                d = r.json().get("data") or {}
                if isinstance(d, list): d = d[0] if d else {}
                return {"exchange": exchange, "oi": float(d.get("holdVol", 0)), "unit": "contracts"}
            elif exchange == "bitget":
                r = await c.get(f"https://api.bitget.com/api/v2/mix/market/open-interest?symbol={symbol}USDT&productType=USDT-FUTURES")
                items = (r.json().get("data") or {}).get("openInterestList", [])
                oi = float(items[0].get("size", 0)) if items else 0
                return {"exchange": exchange, "oi": oi, "unit": "contracts"}
            elif exchange == "kucoin":
                sym = ("XBT" if symbol == "BTC" else symbol) + "USDTM"
                r = await c.get(f"https://api-futures.kucoin.com/api/v1/contracts/{sym}")
                d = r.json().get("data") or {}
                return {"exchange": exchange, "oi": float(d.get("openInterest", 0)), "unit": "contracts"}
            elif exchange == "whitebit":
                r = await c.get(f"https://whitebit.com/api/v4/public/futures")
                items = r.json() if isinstance(r.json(), list) else (r.json().get("result") or r.json().get("data") or [])
                target = f"{symbol}_PERP"
                for it in items:
                    tid = it.get("ticker_id") or it.get("market") or it.get("name") or ""
                    if tid == target:
                        return {"exchange": exchange, "oi": float(it.get("open_interest", 0) or 0), "unit": "contracts"}
    except Exception as exc:
        logger.warning("OI %s/%s failed: %s", exchange, symbol, exc)
    return None


@router.get("/open-interest")
async def open_interest(
    symbol: str = Query(...),
    long_ex: str = Query(...),
    short_ex: str = Query(...),
):
    """Open interest for long and short exchange for a pair."""
    key = (symbol, long_ex, short_ex)
    hit = _oi_cache.get(key)
    if hit and time.time() - hit[1] < _OI_TTL:
        return hit[0]
    results = await asyncio.gather(
        _fetch_open_interest(long_ex, symbol),
        _fetch_open_interest(short_ex, symbol),
        return_exceptions=True,
    )
    out_map = {}
    for ex, res in zip([long_ex, short_ex], results):
        out_map[ex] = res if isinstance(res, dict) else None
    out = {"symbol": symbol, "open_interest": out_map}
    _oi_cache[key] = (out, time.time())
    return out


@router.get("/arb-history")
async def arb_history(
    symbol: str = Query(...),
    long_ex: str = Query(...),
    short_ex: str = Query(...),
):
    key = (symbol, long_ex, short_ex)
    hit = _fh_cache.get(key)
    if hit and time.time() - hit[1] < _FH_TTL:
        return hit[0]
    long_hist, short_hist = await asyncio.gather(
        _fetch_history_for(long_ex, symbol),
        _fetch_history_for(short_ex, symbol),
    )
    out = {
        "symbol": symbol,
        "long_exchange": long_ex,
        "short_exchange": short_ex,
        "long_fee": EXCHANGE_FEES.get(long_ex, 0.0006),
        "short_fee": EXCHANGE_FEES.get(short_ex, 0.0006),
        "long_history": long_hist,
        "short_history": short_hist,
    }
    _fh_cache[key] = (out, time.time())
    return out


# ── WebSocket: live funding rates ──────────────────────────────────────────────

_funding_clients: set[WebSocket] = set()
_arb_clients: set[WebSocket] = set()
_broadcaster_task: asyncio.Task | None = None
# Push to connected WS clients every 1s. We already use diff payloads on
# /ws/arb so the wire cost of this is ~3-10KB per tick (only changed rows).
# /ws/funding sends a full snapshot; each push is ~300KB but gzip-compressed
# it's <100KB and every client handles that in <50ms.
BROADCAST_INTERVAL = _env_float("AVALANT_BROADCAST_INTERVAL", 0.25)
# WS push cadence: 250ms gives sub-second refresh without drowning clients.
# Funding is diff-payloaded so CPU cost per tick is bounded by row-change count;
# at steady-state only a handful of rows move per 250ms window.


async def _push(clients: set[WebSocket], msg: str) -> None:
    dead: set[WebSocket] = set()
    for ws in list(clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    clients -= dead
    if dead:
        logger.debug("Screener WS: removed %d dead connections", len(dead))


async def _warmup() -> None:
    """Background task: pre-fetch interval maps (slow for MEXC/Bitget)."""
    await asyncio.gather(
        *(_get_interval_map(ex) for ex in _IVL_FETCHERS),
        return_exceptions=True,
    )
    logger.info("Screener interval cache warmed up")


_REFRESH_INTERVAL = _env_float("AVALANT_REFRESH_INTERVAL", 0.3)
# arb recompute cadence; _compute_arb_sync runs in a thread so loop stays free.
# 300ms recompute + 250ms broadcast = sub-second freshness end-to-end when WS
# adapters are healthy (their _rows already update in real-time as frames arrive).


async def _refresh_loop() -> None:
    """Recompute arb result from the current funding _cache every 4s.
    Funding fetches run as fire-and-forget background tasks so slow exchanges
    can't stall the recompute — arb always works off whatever rows are cached
    (the fetches update _cache asynchronously)."""
    from backend.services.alpha_service import score_opportunities
    from backend.services.arbitrage_service import (
        FETCHERS, _cache, _arb_result_cache, _compute_arb_sync,
        _write_file_cache, _read_file_cache,
        _write_file_cache_async, _read_file_cache_async,
        get_funding_data, _slim_arb_for_file,
        _mono, CACHE_TTL, _drop_price_outliers,
    )
    from backend.services import admin_settings
    _fetch_lock = asyncio.Lock()
    _compute_lock = asyncio.Lock()
    async def _single_fetch():
        if _fetch_lock.locked():
            return  # previous fetch still running — skip
        async with _fetch_lock:
            try:
                await get_funding_data()
            except Exception as exc:
                logger.warning("Background funding fetch: %s", exc)

    # How old a per-exchange local cache can be before we prefer the
    # shared funding.json (written by whichever worker last succeeded).
    # Fetchers refresh every ~3-6s; 15s gives ~2-3 missed ticks before we
    # reach over to another worker's snapshot.
    _LOCAL_STALE_MAX = 15.0

    while True:
        started = asyncio.get_event_loop().time()
        # Kick background funding refresh — never await. Lock ensures only one
        # get_funding_data runs at a time (prevents pool exhaustion / duplicates).
        asyncio.create_task(_single_fetch())
        try:
            disabled_ex = admin_settings.get_disabled_exchanges()
            hidden_sym = admin_settings.get_hidden_symbols()
            min_volume = admin_settings.get_arb_min_volume_usd()
            shared = await _read_file_cache_async("funding.json", max_age=30.0) or {}
            shared_rows_by_ex: dict[str, list] = {}
            for r in shared.get("rows", []) or []:
                shared_rows_by_ex.setdefault(r.get("exchange",""), []).append(r)
            now_m = _mono()
            rows = []
            for ex in FETCHERS:
                if ex in disabled_ex:
                    continue
                cached_rows, cached_ts = _cache.get(ex, ([], 0.0))
                age = now_m - cached_ts if cached_ts else float("inf")
                if cached_rows and age <= _LOCAL_STALE_MAX:
                    rows.extend(cached_rows)
                elif ex in shared_rows_by_ex:
                    rows.extend(shared_rows_by_ex[ex])
                elif cached_rows:
                    rows.extend(cached_rows)

            def _keep(r: dict) -> bool:
                # Mirrors arbitrage_service.get_funding_data's filter —
                # keep in sync. Zero rate = uninitialised / stale feed;
                # no venue reports a truly-zero funding in practice.
                if hidden_sym and r.get("symbol") in hidden_sym:
                    return False
                v = r.get("volume_usd")
                if v is None:
                    return False
                rate = r.get("rate")
                if rate is None:
                    return False
                try:
                    if float(rate) == 0.0:
                        return False
                    return float(v) >= min_volume
                except (TypeError, ValueError):
                    return False
            rows = [r for r in rows if _keep(r)]
            rows = _drop_price_outliers(rows)

            # Out-of-process compute: if AVALANT_ARB_COMPUTE_MODE=subprocess
            # AND the worker is alive, skip the in-master compute path
            # entirely — the subprocess owns arbitrage.json writes. Pick up
            # its latest result from the file cache so alpha-score + the
            # in-memory cache for WS broadcasters stays in sync.
            from backend.services.arb_compute_service import (
                is_subprocess_mode as _is_sp, is_worker_alive as _worker_alive,
            )
            if _is_sp() and _worker_alive():
                try:
                    latest = await _read_file_cache_async("arbitrage.json", max_age=10.0)
                    if latest and latest.get("opportunities") is not None:
                        _arb_result_cache["data"] = latest
                        _arb_result_cache["ts"] = time.time()
                        score_opportunities(latest.get("opportunities", []))
                except Exception as exc:
                    logger.debug("pickup arbitrage.json failed: %s", exc)
                elapsed = asyncio.get_event_loop().time() - started
                await asyncio.sleep(max(0.1, _REFRESH_INTERVAL - elapsed))
                continue

            if rows and not _compute_lock.locked():
                # Prevent queue buildup: if the previous compute is still
                # running (to_thread future hasn't resolved), skip this tick.
                # Default asyncio to_thread uses a shared threadpool and
                # Python's GIL, so piling 5+ computes queued doesn't parallelise
                # — it just starves the event loop until they all drain.
                async with _compute_lock:
                    result = await asyncio.to_thread(_compute_arb_sync, rows, time.time())
                # Anti-flicker: transient WS / orderbook hiccups can cause the
                # compute to drop to a fraction of its usual pair count for
                # 1-2 ticks. Publishing those would make the UI blink "No data
                # yet" before recovering. If the new result has <50% of the
                # previous opp count AND the previous result is still fresh
                # (<5s), keep the previous result for this tick.
                new_count = len(result.get("opportunities", []))
                prev = _arb_result_cache.get("data")
                prev_count = len(prev.get("opportunities", [])) if prev else 0
                prev_age = time.time() - (_arb_result_cache.get("ts") or 0)
                if (prev_count > 0
                        and new_count < prev_count * 0.5
                        and prev_age < 5.0):
                    logger.info(
                        "arb anti-flicker: skipped write (prev=%d new=%d age=%.1fs)",
                        prev_count, new_count, prev_age,
                    )
                else:
                    _arb_result_cache["data"] = result
                    _arb_result_cache["ts"] = time.time()
                    await _write_file_cache_async("arbitrage.json", _slim_arb_for_file(result))
                    score_opportunities(result.get("opportunities", []))
        except Exception as exc:
            logger.warning("Refresh arb error: %s", exc)
        elapsed = asyncio.get_event_loop().time() - started
        await asyncio.sleep(max(0.1, _REFRESH_INTERVAL - elapsed))


# ── Arb diff state (per worker) ──────────────────────────────────────────────
# Remembers the last broadcast snapshot so we can send only the delta each
# tick. Keyed by (symbol, long_exchange, short_exchange). Per-worker — each
# uvicorn worker tracks its own clients and their last-seen state.
_last_arb_broadcast: dict[tuple, dict] = {}
_last_arb_meta: dict = {"ts": 0.0, "fees": {}, "exchanges": []}

# ── Funding diff state (per worker) ──────────────────────────────────────────
# Funding payloads are ~1 MB per tick at ~5 000 rows. Full push at 0.3 s
# broadcast cadence = ~3 MB/s × N clients — blows through 800 Mbps fast.
# Keyed by (exchange, symbol), diff shape matches the arb diff so the
# frontend gets a single update/remove path.
_last_funding_broadcast: dict[tuple, dict] = {}
_FUNDING_DIFF_FIELDS = ("rate", "price", "volume_usd", "next_ts", "interval_h", "apr")


def _funding_key(r: dict) -> tuple:
    return (r.get("exchange"), r.get("symbol"))


def _funding_differs(a: dict, b: dict) -> bool:
    for k in _FUNDING_DIFF_FIELDS:
        if a.get(k) != b.get(k):
            return True
    return False

# Fields that matter for an "updated" decision. Everything else is either
# derivative (apr) or identity (symbol, exchanges — already the key).
_ARB_DIFF_FIELDS = (
    "net_profit", "gross_funding", "price_spread", "total_fees",
    "long_price", "short_price", "long_rate", "short_rate",
    "long_volume", "short_volume",
    "next_ts_long", "next_ts_short", "valid_price",
    "in_pct", "out_pct", "alpha_score",
)


def _arb_key(o: dict) -> tuple:
    return (o.get("symbol"), o.get("long_exchange"), o.get("short_exchange"))


def _opps_differ(a: dict, b: dict) -> bool:
    """Fast field-level compare. Used to filter "updated" to rows that
    actually changed on something the UI renders."""
    for k in _ARB_DIFF_FIELDS:
        if a.get(k) != b.get(k):
            return True
    return False


def _build_arb_snapshot_payload(data: dict) -> str:
    """Snapshot message sent to brand-new WS clients so they can paint
    the initial table. Shape matches the legacy broadcast exactly +
    a `type` tag so the frontend can branch on it."""
    payload = {
        "type": "snapshot",
        "ts":   data.get("ts"),
        "fees": data.get("fees", {}),
        "exchanges":     data.get("exchanges", []),
        "opportunities": data.get("opportunities", []),
    }
    if "truncated_to" in data:
        payload["truncated_to"] = data["truncated_to"]
    if "full_count" in data:
        payload["full_count"] = data["full_count"]
    return json.dumps(payload)


_last_arb_broadcast_at: float = 0.0


def _build_funding_snapshot_payload(data: dict) -> str:
    """Full snapshot for new funding WS clients. Same envelope the frontend
    has always parsed — keep the `rows` + meta fields, add a `type` tag."""
    payload = {
        "type": "snapshot",
        "ts": data.get("ts"),
        "rows": data.get("rows") or [],
        "exchanges": data.get("exchanges", []),
    }
    return json.dumps(payload)


def _build_funding_diff(curr: dict) -> dict | None:
    """Delta against `_last_funding_broadcast`. On big drops (likely WS
    dropout), suppress the push so the user doesn't see rows flicker out
    and back in."""
    global _last_funding_broadcast
    curr_rows = curr.get("rows") or []
    curr_by_key = {_funding_key(r): r for r in curr_rows if r.get("symbol") and r.get("exchange")}

    prev_count = len(_last_funding_broadcast)
    new_count = len(curr_by_key)
    if prev_count > 100 and new_count < prev_count * 0.5:
        # Same empty-guard pattern as arb broadcast: don't trust a
        # transient drop to more than half the row count.
        return None

    added, updated = [], []
    for k, r in curr_by_key.items():
        prev = _last_funding_broadcast.get(k)
        if prev is None:
            added.append(r)
        elif _funding_differs(prev, r):
            updated.append(r)
    removed = [list(k) for k in _last_funding_broadcast.keys() if k not in curr_by_key]

    if not added and not updated and not removed:
        return None

    payload: dict = {"type": "diff", "ts": curr.get("ts") or time.time()}
    if added:   payload["added"]   = added
    if updated: payload["updated"] = updated
    if removed: payload["removed"] = removed

    _last_funding_broadcast = curr_by_key
    return payload


def _build_arb_diff(curr: dict) -> dict | None:
    """Compute the delta between the current computed arb result and the
    last one we broadcast. Returns None if literally nothing changed —
    the broadcaster skips the push entirely on no-ops to save bandwidth.

    Empty-snapshot guard: if the computed result has <50% of the previous
    opp count AND the previous broadcast was recent (<5s), we suppress the
    push. A momentary gap in the fetcher's refresh cycle would otherwise
    translate to `removed: [hundreds of keys]` and wipe the user's grid
    for one tick before it recovers on the next one. Users were seeing
    the screener flash empty — this is its immediate cause.
    """
    global _last_arb_broadcast, _last_arb_meta, _last_arb_broadcast_at
    curr_opps = curr.get("opportunities", []) or []
    curr_by_key = {_arb_key(o): o for o in curr_opps}

    prev_count = len(_last_arb_broadcast)
    new_count = len(curr_by_key)
    now_ts = time.time()
    if (prev_count > 0
            and new_count < prev_count * 0.5
            and (now_ts - _last_arb_broadcast_at) < 5.0):
        logger.info(
            "arb broadcast empty-guard: skipped push (prev=%d new=%d age=%.1fs)",
            prev_count, new_count, now_ts - _last_arb_broadcast_at,
        )
        return None

    added, updated = [], []
    for k, o in curr_by_key.items():
        prev = _last_arb_broadcast.get(k)
        if prev is None:
            added.append(o)
        elif _opps_differ(prev, o):
            updated.append(o)
    removed = [list(k) for k in _last_arb_broadcast.keys() if k not in curr_by_key]

    fees_now = curr.get("fees", {})
    exchanges_now = curr.get("exchanges", [])
    meta_changed = (
        fees_now != _last_arb_meta.get("fees")
        or exchanges_now != _last_arb_meta.get("exchanges")
    )

    if not added and not updated and not removed and not meta_changed:
        return None

    payload: dict = {
        "type": "diff",
        "ts":   curr.get("ts") or time.time(),
    }
    if added:    payload["added"]   = added
    if updated:  payload["updated"] = updated
    if removed:  payload["removed"] = removed
    if meta_changed:
        payload["fees"]      = fees_now
        payload["exchanges"] = exchanges_now

    _last_arb_broadcast = curr_by_key
    _last_arb_meta = {"ts": curr.get("ts"), "fees": fees_now, "exchanges": exchanges_now}
    _last_arb_broadcast_at = now_ts
    return payload


HOT_PAIRS_WARMUP_INTERVAL = 300  # seconds — every 5 min
HOT_PAIRS_COUNT = 20


async def _warm_hot_pair(symbol: str, long_ex: str, short_ex: str) -> None:
    """Populate _ph_cache, _fh_cache, _oi_cache for one pair."""
    key = (symbol, long_ex, short_ex)
    try:
        if not _ph_cache.get(key) or time.time() - _ph_cache[key][1] > _PH_TTL / 2:
            long_p, short_p = await asyncio.gather(
                _fetch_price_history(long_ex, symbol),
                _fetch_price_history(short_ex, symbol),
            )
            _ph_cache[key] = ({
                "symbol": symbol, "long_exchange": long_ex, "short_exchange": short_ex,
                "long_prices": long_p, "short_prices": short_p,
            }, time.time())
        if not _fh_cache.get(key) or time.time() - _fh_cache[key][1] > _FH_TTL / 2:
            long_h, short_h = await asyncio.gather(
                _fetch_history_for(long_ex, symbol),
                _fetch_history_for(short_ex, symbol),
            )
            _fh_cache[key] = ({
                "symbol": symbol, "long_exchange": long_ex, "short_exchange": short_ex,
                "long_fee": EXCHANGE_FEES.get(long_ex, 0.0006),
                "short_fee": EXCHANGE_FEES.get(short_ex, 0.0006),
                "long_history": long_h, "short_history": short_h,
            }, time.time())
        if not _oi_cache.get(key) or time.time() - _oi_cache[key][1] > _OI_TTL / 2:
            oi_l, oi_s = await asyncio.gather(
                _fetch_open_interest(long_ex, symbol),
                _fetch_open_interest(short_ex, symbol),
                return_exceptions=True,
            )
            _oi_cache[key] = ({
                "symbol": symbol,
                "open_interest": {
                    long_ex:  oi_l if isinstance(oi_l, dict) else None,
                    short_ex: oi_s if isinstance(oi_s, dict) else None,
                },
            }, time.time())
    except Exception as exc:
        logger.debug("Hot-pair warmup %s %s>%s failed: %s", symbol, long_ex, short_ex, exc)


async def _warm_hot_pairs_loop() -> None:
    """Periodically pre-warm per-pair caches for the top-N arb opportunities."""
    while True:
        try:
            data = await get_arbitrage_opportunities()
            opps = data.get("opportunities", [])[:HOT_PAIRS_COUNT]
            if opps:
                # run 4 in parallel to avoid hammering
                sem = asyncio.Semaphore(4)
                async def _one(o):
                    async with sem:
                        await _warm_hot_pair(o["symbol"], o["long_exchange"], o["short_exchange"])
                await asyncio.gather(*(_one(o) for o in opps), return_exceptions=True)
                logger.debug("Hot-pairs warmup: %d pairs primed", len(opps))
        except Exception as exc:
            logger.warning("Hot-pairs warmup loop error: %s", exc)
        await asyncio.sleep(HOT_PAIRS_WARMUP_INTERVAL)


async def _broadcast_loop() -> None:
    """Push cached data to WS clients on THIS worker every BROADCAST_INTERVAL.
    Runs on every worker — each one reads from the shared file cache populated
    by the refresh loop (which runs on only one worker via file lock)."""
    from backend.services.arbitrage_service import _arb_result_cache, _read_file_cache
    # Pre-warm per-pair caches for top opportunities (price-history,
    # funding-history, OI). First visitor to any hot pair hits warm cache.
    asyncio.create_task(_warm_hot_pairs_loop())

    while True:
        await asyncio.sleep(BROADCAST_INTERVAL)
        # Funding payload — diff-only. Previously we pushed the full ~1 MB
        # snapshot every BROADCAST_INTERVAL (3 MB/s × N clients). Now we
        # track last-seen rows per-worker and send only rate/price/volume
        # updates, mirroring the arb diff shape.
        try:
            if _funding_clients:
                fd = _read_file_cache("funding.json", max_age=60)
                if not fd:
                    fd = await get_funding_data()
                if fd:
                    diff = _build_funding_diff(fd)
                    if diff is not None:
                        await _push(_funding_clients, _fast_dumps(diff))
        except Exception as exc:
            logger.debug("Funding push skipped: %s", exc)
        # Arb payload — send diff-only (massively smaller than a full snapshot
        # when the top-N is mostly stable). New WS clients still receive a
        # "snapshot" message on connect, so they have something to paint.
        try:
            data = _read_file_cache("arbitrage.json", max_age=60)
            if not data:
                data = _arb_result_cache.get("data")
            if data and _arb_clients:
                diff = _build_arb_diff(data)
                if diff is not None:
                    await _push(_arb_clients, _fast_dumps(diff))
        except Exception as exc:
            logger.debug("Arb push skipped: %s", exc)


def start_screener_broadcaster() -> None:
    """Start broadcaster on EVERY worker. Only one worker (lock-holder) also
    runs the refresh loop which writes funding.json + arbitrage.json; every
    other worker reads those files and pushes to its own local WS clients.

    Kept for back-compat with monolith deploys. When running sidecar'd
    (AVALANT_ROLE=fetcher / AVALANT_ROLE=web), prefer the narrower
    start_refresh_loop / start_broadcast_loop helpers below.
    """
    start_broadcast_loop()
    start_refresh_loop()


def start_broadcast_loop() -> None:
    """Web-worker half: pushes the currently cached arb + funding data
    to connected WS clients every BROADCAST_INTERVAL. Does NOT touch any
    external network — reads only shared files written by the fetcher.
    Safe to run on every uvicorn worker."""
    global _broadcaster_task
    if _broadcaster_task and not _broadcaster_task.done():
        return
    _broadcaster_task = asyncio.create_task(_broadcast_loop())


def start_refresh_loop() -> None:
    """Fetcher-side half: recomputes arb opps from the shared funding
    cache every _REFRESH_INTERVAL and writes arbitrage.json. Acquires a
    file-lock so it's safe to call from multiple processes — only the
    first caller wins.
    """
    import fcntl
    global _refresh_task, _refresh_lock_fd
    if _refresh_task and not _refresh_task.done():
        return
    try:
        _refresh_lock_fd = open("/tmp/avalant_refresh.lock", "w")
        fcntl.flock(_refresh_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        logger.info("Screener refresh: another worker/process holds the lock — skipping")
        return
    asyncio.create_task(_warmup())
    _refresh_task = asyncio.create_task(_refresh_loop())
    logger.info("Screener refresh loop started (this process drives recompute)")


def stop_refresh_loop() -> None:
    global _refresh_task, _refresh_lock_fd
    if _refresh_task and not _refresh_task.done():
        _refresh_task.cancel()
    _refresh_task = None
    if _refresh_lock_fd is not None:
        try:
            _refresh_lock_fd.close()
        except Exception:
            pass
        _refresh_lock_fd = None


def stop_broadcast_loop() -> None:
    global _broadcaster_task
    if _broadcaster_task and not _broadcaster_task.done():
        _broadcaster_task.cancel()
    _broadcaster_task = None


_refresh_lock_fd = None
_refresh_task: asyncio.Task | None = None


def stop_screener_broadcaster() -> None:
    global _broadcaster_task, _refresh_task
    for t in (_broadcaster_task, _refresh_task):
        if t:
            t.cancel()
    _broadcaster_task = None
    _refresh_task = None


async def _ws_authenticate(websocket: WebSocket, label: str) -> int | None:
    """Auth via first-frame {"auth": "<JWT>"} after accept().

    The JWT used to be passed as ?token= in the URL — that put it into nginx
    access logs (token leak). First-frame auth keeps the token in the WS
    payload only. 5 s wait window; on timeout / bad payload the socket is
    closed with code 4401 and we never reach the streaming loop.
    """
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        try: await websocket.close(code=4401, reason="auth timeout")
        except Exception: pass
        return None
    token = ""
    try:
        msg = json.loads(raw)
        if isinstance(msg, dict):
            token = str(msg.get("auth") or "").strip()
    except (ValueError, TypeError):
        pass
    if not token:
        try: await websocket.close(code=4401, reason="auth required")
        except Exception: pass
        logger.debug("%s WS rejected — no auth frame", label)
        return None
    user_id = decode_token(token)
    if not user_id:
        try: await websocket.close(code=4401, reason="invalid token")
        except Exception: pass
        logger.debug("%s WS rejected — invalid token", label)
        return None
    return user_id


async def _ws_handler(websocket: WebSocket, clients: set[WebSocket],
                      fetch_fn, label: str,
                      snapshot_builder=None) -> None:
    await websocket.accept()
    user_id = await _ws_authenticate(websocket, label)
    if user_id is None:
        return
    clients.add(websocket)
    logger.debug("Screener %s WS connect uid=%s (total=%d)", label, user_id, len(clients))

    try:
        data = await fetch_fn()
        # Snapshot-builder hook: lets the arb channel send a typed
        # "snapshot" message so the client knows diffs will follow. Legacy
        # channels (funding) fall back to raw data — unchanged wire shape.
        first_payload = snapshot_builder(data) if snapshot_builder else None
        if first_payload is not None:
            await websocket.send_text(first_payload)
        else:
            await websocket.send_json(data)
        while True:
            text = await websocket.receive_text()
            if text == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("Screener %s WS error uid=%s: %s", label, user_id, exc)
    finally:
        clients.discard(websocket)
        logger.debug("Screener %s WS disconnect uid=%s (total=%d)", label, user_id, len(clients))


@router.websocket("/ws/funding")
async def funding_ws(websocket: WebSocket) -> None:
    await _ws_handler(
        websocket, _funding_clients, get_funding_data, "funding",
        snapshot_builder=_build_funding_snapshot_payload,
    )


@router.websocket("/ws/long-short")
async def long_short_ws(websocket: WebSocket) -> None:
    """Canonical WS for the Long/Short feed."""
    await _ws_handler(
        websocket, _arb_clients, get_arbitrage_opportunities, "long-short",
        snapshot_builder=_build_arb_snapshot_payload,
    )


@router.websocket("/ws/arb")
async def arb_ws(websocket: WebSocket) -> None:
    """Legacy alias for /ws/long-short. Kept so existing frontend connections
    don't break while we roll out the rename."""
    await _ws_handler(
        websocket, _arb_clients, get_arbitrage_opportunities, "arb",
        snapshot_builder=_build_arb_snapshot_payload,
    )


# ── Orderbook WS push ─────────────────────────────────────────────────────────
# Per-client subscription registry. Each ws → {pair: last_ts_sent}. The
# broadcaster task reads the shared file-cache every BOOK_BROADCAST_INTERVAL
# and diff-pushes updated pairs only.
#
# Why this exists: /arb previously polled /api/screener/orderbook every 150ms
# per side — at 500 concurrent users that's 6600 req/s against the web role's
# cache lookup. This WS push is fed by the same _file_memo so exchange API
# traffic is unchanged (zero extra calls to Binance/Bybit/etc).
_book_ws_subs: dict[WebSocket, dict[str, float]] = {}
_book_broadcast_task: asyncio.Task | None = None
BOOK_BROADCAST_INTERVAL = _env_float("AVALANT_BOOK_BROADCAST_INTERVAL", 0.1)
BOOK_MAX_PAIRS_PER_CLIENT = 100  # /arb needs 2, /screener live In/Out needs ~80 for top-40 rows


async def _book_broadcast_loop() -> None:
    """Push fresh orderbook frames to subscribed clients. Reads the shared
    books.json via orderbook_cache._refresh_file_memo — no exchange calls.

    NOTE: we import the module (not its _file_memo binding) because
    _refresh_file_memo rebinds the module-level name on every reload — a
    `from … import _file_memo` at top level would freeze our reference to
    the initial (empty) dict."""
    from backend.services import orderbook_cache as _ob
    while True:
        try:
            await asyncio.sleep(BOOK_BROADCAST_INTERVAL)
            if not _book_ws_subs:
                continue
            _ob._refresh_file_memo()
            for ws, subs in list(_book_ws_subs.items()):
                if not subs:
                    continue
                payload: dict[str, dict] = {}
                for pair, last_ts in list(subs.items()):
                    entry = _ob._file_memo.get(pair)
                    if not entry:
                        continue
                    ts = entry.get("ts", 0.0)
                    if ts <= last_ts:
                        continue
                    data = entry.get("data") or {}
                    payload[pair] = {
                        "ts": ts,
                        "bids": data.get("bids") or [],
                        "asks": data.get("asks") or [],
                    }
                    subs[pair] = ts
                if payload:
                    try:
                        await ws.send_json({"books": payload})
                    except Exception:
                        # Client gone or wire error — drop the sub; next send will
                        # fully clean up on receive-loop side.
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("book broadcast: %s", exc)


def start_book_broadcast_loop() -> None:
    """Web-worker half: pushes live orderbook frames to subscribed clients.
    Reads only the shared books.json, no exchange calls — safe on every
    uvicorn worker."""
    global _book_broadcast_task
    if _book_broadcast_task and not _book_broadcast_task.done():
        return
    _book_broadcast_task = asyncio.create_task(_book_broadcast_loop())


def stop_book_broadcast_loop() -> None:
    global _book_broadcast_task
    if _book_broadcast_task and not _book_broadcast_task.done():
        _book_broadcast_task.cancel()
    _book_broadcast_task = None


def _normalize_pair(raw: str) -> str | None:
    """pair = 'exchange:SYMBOL' — matches the _book_cache key format."""
    if not raw or ":" not in raw:
        return None
    ex, _, sym = raw.partition(":")
    ex = ex.strip().lower()
    sym = sym.strip().upper()
    if not ex or not sym or len(ex) > 24 or len(sym) > 16:
        return None
    if not ex.replace("_", "").isalnum() or not sym.replace("_", "").isalnum():
        return None
    return f"{ex}:{sym}"


@router.websocket("/ws/book")
async def book_ws(websocket: WebSocket) -> None:
    """Live orderbook push for /arb.

    Protocol:
      Client → {"auth": "<JWT>"}                              (first frame, required)
              {"action": "subscribe",   "pairs": ["binance:BTC", "bybit:BTC"]}
              {"action": "unsubscribe", "pairs": [...]}
              (text "ping" → text "pong" heartbeat accepted too)
      Server → {"books": {"<ex>:<SYM>": {ts, bids, asks}, ...}}

    Subscribing a pair also kicks the orderbook prewarm poller so pairs
    outside the top-N prewarm set start flowing into books.json within one
    POLL_INTERVAL (~500ms)."""
    await websocket.accept()
    user_id = await _ws_authenticate(websocket, "book")
    if user_id is None:
        return
    _book_ws_subs[websocket] = {}
    logger.debug("book WS connect uid=%s (total=%d)", user_id, len(_book_ws_subs))
    try:
        while True:
            raw = await websocket.receive_text()
            if raw == "ping":
                await websocket.send_text("pong")
                continue
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            action = (msg.get("action") or "").lower()
            pairs_raw = msg.get("pairs") or []
            if not isinstance(pairs_raw, list):
                continue
            pairs = [p for p in (_normalize_pair(x) for x in pairs_raw if isinstance(x, str)) if p]
            subs = _book_ws_subs.get(websocket)
            if subs is None:
                break
            if action == "subscribe":
                # Cap total subs per client so a misbehaving tab can't pin
                # all server-side book memory.
                free = max(0, BOOK_MAX_PAIRS_PER_CLIENT - len(subs))
                for pair in pairs[:free]:
                    subs[pair] = 0.0
                    ex, _, sym = pair.partition(":")
                    try:
                        from backend.services.orderbook_cache import (
                            get_cached_orderbook, touch_user_sub,
                        )
                        # Fire REST fallback on this worker for instant first paint.
                        asyncio.create_task(get_cached_orderbook(ex, sym, 50))
                        # Record the pair in the cross-worker user-hot-list so the
                        # prewarm owner keeps the WS subscribed across prune cycles.
                        touch_user_sub(ex, sym)
                    except Exception:
                        pass
                logger.debug("book WS subscribe uid=%s pairs=%s total=%d",
                             user_id, pairs[:free], len(subs))
            elif action == "unsubscribe":
                for pair in pairs:
                    subs.pop(pair, None)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("book WS error uid=%s: %s", user_id, exc)
    finally:
        _book_ws_subs.pop(websocket, None)
        logger.debug("book WS disconnect uid=%s (total=%d)", user_id, len(_book_ws_subs))
