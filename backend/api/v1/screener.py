import asyncio
import json
import logging
import time

import httpx
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from backend.api.deps import get_current_user
from backend.services.arbitrage_service import (
    get_arbitrage_opportunities, get_funding_data, _get_interval_map, _IVL_FETCHERS, _cache,
    EXCHANGE_FEES, _http as _arb_http,
)
from backend.services.auth_service import decode_token

router = APIRouter(prefix="/screener", tags=["screener"])
logger = logging.getLogger("avalant.screener")

# ── REST endpoints ─────────────────────────────────────────────────────────────

@router.get("/funding")
async def funding_rates():
    """Funding rates across perpetual futures exchanges. Cached 30s per exchange."""
    return await get_funding_data()


@router.get("/arbitrage")
async def arbitrage_opportunities():
    """Cross-exchange funding arbitrage opportunities with price spread and fees."""
    return await get_arbitrage_opportunities()


_availability_cache: dict = {"data": None, "ts": 0.0}
_AVAILABILITY_TTL = 10.0


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
        c = _arb_http  # reuse persistent pool
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
        c = _arb_http  # reuse persistent pool
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
    long_prices, short_prices = await asyncio.gather(
        _fetch_price_history(long_ex, symbol),
        _fetch_price_history(short_ex, symbol),
    )
    return {
        "symbol": symbol,
        "long_exchange": long_ex,
        "short_exchange": short_ex,
        "long_prices": long_prices,
        "short_prices": short_prices,
    }


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
        c = _arb_http  # reuse persistent pool
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
    results = await asyncio.gather(
        _fetch_open_interest(long_ex, symbol),
        _fetch_open_interest(short_ex, symbol),
        return_exceptions=True,
    )
    out = {}
    for ex, res in zip([long_ex, short_ex], results):
        if isinstance(res, dict):
            out[ex] = res
        else:
            out[ex] = None
    return {"symbol": symbol, "open_interest": out}


@router.get("/arb-history")
async def arb_history(
    symbol: str = Query(...),
    long_ex: str = Query(...),
    short_ex: str = Query(...),
):
    long_hist, short_hist = await asyncio.gather(
        _fetch_history_for(long_ex, symbol),
        _fetch_history_for(short_ex, symbol),
    )
    return {
        "symbol": symbol,
        "long_exchange": long_ex,
        "short_exchange": short_ex,
        "long_fee": EXCHANGE_FEES.get(long_ex, 0.0006),
        "short_fee": EXCHANGE_FEES.get(short_ex, 0.0006),
        "long_history": long_hist,
        "short_history": short_hist,
    }


# ── WebSocket: live funding rates ──────────────────────────────────────────────

_funding_clients: set[WebSocket] = set()
_arb_clients: set[WebSocket] = set()
_broadcaster_task: asyncio.Task | None = None
BROADCAST_INTERVAL = 3  # seconds — full arb list refresh every 3s


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


_REFRESH_INTERVAL = 3.0  # recompute + push cadence (seconds)


async def _refresh_loop() -> None:
    """Recompute arb result from the current funding _cache every 4s.
    Funding fetches run as fire-and-forget background tasks so slow exchanges
    can't stall the recompute — arb always works off whatever rows are cached
    (the fetches update _cache asynchronously)."""
    from backend.services.alpha_service import score_opportunities
    from backend.services.arbitrage_service import (
        FETCHERS, _cache, _arb_result_cache, _compute_arb_sync,
        _write_file_cache, _read_file_cache, get_funding_data, _slim_arb_for_file,
        _mono, CACHE_TTL,
    )
    from backend.services import admin_settings
    _fetch_lock = asyncio.Lock()
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
        # Recompute arb. Prefer local _cache when it's fresh (< 20s) because
        # that's the hottest data we have. If an exchange keeps timing out on
        # THIS worker (e.g. KuCoin ConnectTimeouts on the owner), its local
        # cache goes stale — fall back to the shared funding.json, which
        # another worker may have refreshed successfully. Without this fallback
        # the owner kept publishing arb rows with days-old prices for any
        # exchange that only this worker struggled with.
        try:
            disabled_ex = admin_settings.get_disabled_exchanges()
            hidden_sym = admin_settings.get_hidden_symbols()
            shared = _read_file_cache("funding.json", max_age=30.0) or {}
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
                    # No shared fallback either — keep stale, better than empty
                    rows.extend(cached_rows)
            if hidden_sym:
                rows = [r for r in rows if r["symbol"] not in hidden_sym]
            if rows:
                result = await asyncio.to_thread(_compute_arb_sync, rows, time.time())
                _arb_result_cache["data"] = result
                _arb_result_cache["ts"] = time.time()
                _write_file_cache("arbitrage.json", _slim_arb_for_file(result))
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


def _build_arb_diff(curr: dict) -> dict | None:
    """Compute the delta between the current computed arb result and the
    last one we broadcast. Returns None if literally nothing changed —
    the broadcaster skips the push entirely on no-ops to save bandwidth.
    """
    global _last_arb_broadcast, _last_arb_meta
    curr_opps = curr.get("opportunities", []) or []
    curr_by_key = {_arb_key(o): o for o in curr_opps}
    added, updated = [], []
    for k, o in curr_by_key.items():
        prev = _last_arb_broadcast.get(k)
        if prev is None:
            added.append(o)
        elif _opps_differ(prev, o):
            updated.append(o)
    removed = [list(k) for k in _last_arb_broadcast.keys() if k not in curr_by_key]

    # Meta change (fees dict / exchanges list) triggers a light refresh.
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

    # Rebase our "last" state to the new snapshot — next tick's diff is
    # relative to THIS state.
    _last_arb_broadcast = curr_by_key
    _last_arb_meta = {"ts": curr.get("ts"), "fees": fees_now, "exchanges": exchanges_now}
    return payload


async def _broadcast_loop() -> None:
    """Push cached data to WS clients on THIS worker every BROADCAST_INTERVAL.
    Runs on every worker — each one reads from the shared file cache populated
    by the refresh loop (which runs on only one worker via file lock)."""
    from backend.services.arbitrage_service import _arb_result_cache, _read_file_cache

    while True:
        await asyncio.sleep(BROADCAST_INTERVAL)
        # Funding payload — unchanged (still a full snapshot every tick;
        # the funding page itself is less hot than arb).
        try:
            if _funding_clients:
                fd = _read_file_cache("funding.json", max_age=60)
                if not fd:
                    fd = await get_funding_data()
                if fd:
                    await _push(_funding_clients, json.dumps(fd))
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
                    await _push(_arb_clients, json.dumps(diff))
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


async def _ws_handler(websocket: WebSocket, clients: set[WebSocket], token: str,
                      fetch_fn, label: str,
                      snapshot_builder=None) -> None:
    user_id = decode_token(token) if token else None
    await websocket.accept()
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
async def funding_ws(websocket: WebSocket, token: str = Query("")) -> None:
    await _ws_handler(websocket, _funding_clients, token, get_funding_data, "funding")


@router.websocket("/ws/arb")
async def arb_ws(websocket: WebSocket, token: str = Query("")) -> None:
    await _ws_handler(
        websocket, _arb_clients, token, get_arbitrage_opportunities, "arb",
        snapshot_builder=_build_arb_snapshot_payload,
    )
