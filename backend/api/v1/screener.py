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
from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from backend.services import rate_limit as _rl


def _enforce_screener_rl(request: Request) -> None:
    """Rate-limit dep for public screener feeds. 120/min/IP — well above
    legit poll cadence (frontend uses WS for live), well below what an
    L7 flood needs to soak the _arb_http pool."""
    _rl.enforce("screener_public", request)


def _read_ws_dump_for(exchange: str, cache_dir: str = "/tmp/avalant_cache") -> tuple[list, float]:
    """Read the per-exchange WS subprocess dump. Returns (rows, wall-clock ts).
    On any read/parse error returns ([], 0.0); the caller treats 0.0 as 'unknown'.
    Module-level so refresh_loop closures don't shadow `json` import."""
    try:
        with open(f"{cache_dir}/funding_ws.{exchange}.json", "rb") as f:
            d = json.loads(f.read())
    except FileNotFoundError:
        return ([], 0.0)
    except Exception as exc:
        logging.getLogger("avalant.screener").debug("ws dump read %s: %s", exchange, exc)
        return ([], 0.0)
    tbe = d.get("ts_by_ex") or {}
    wall_ts = tbe.get(exchange) or d.get("ts") or 0.0
    rows_map = d.get("rows") or {}
    ex_rows = rows_map.get(exchange) if isinstance(rows_map, dict) else []
    return (ex_rows or [], float(wall_ts))


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

@router.get("/funding", dependencies=[Depends(_enforce_screener_rl)])
async def funding_rates():
    """Funding rates across perpetual futures exchanges. Cached 30s per exchange.

    Fast path: when the snapshot cache has a pre-serialised byte buffer,
    return it via Response() directly — skips FastAPI's auto-JSON
    serialisation (~80ms saved on 900KB payload). Falls back to dict if
    miss, where the service builds + caches both forms.
    """
    from fastapi.responses import Response
    from backend.services.arbitrage_service import get_funding_data_bytes
    body = await get_funding_data_bytes()
    if body is not None:
        return Response(content=body, media_type="application/json")
    return await get_funding_data()


@router.get("/long-short", dependencies=[Depends(_enforce_screener_rl)])
async def long_short_opportunities():
    """Cross-exchange funding arbitrage (perp-long vs perp-short) with price spread + fees.
    Canonical endpoint for the Long/Short mode."""
    return await get_arbitrage_opportunities()


@router.get("/arbitrage", deprecated=True, dependencies=[Depends(_enforce_screener_rl)])
async def arbitrage_opportunities():
    """Legacy alias for /long-short. Kept so existing bookmarks / API clients
    don't 404 after the rename. Remove once frontend callers are migrated."""
    return await get_arbitrage_opportunities()


@router.get("/spot-short", dependencies=[Depends(_enforce_screener_rl)])
async def spot_short_opportunities():
    """Spot-short cash-and-carry: buy spot on one venue, short perp on another.
    Canonical endpoint."""
    from backend.services.spot_arbitrage_service import get_spot_arbitrage_opportunities
    return await get_spot_arbitrage_opportunities()


@router.get("/spot-arbitrage", deprecated=True, dependencies=[Depends(_enforce_screener_rl)])
async def spot_arbitrage_opportunities():
    """Legacy alias for /spot-short."""
    from backend.services.spot_arbitrage_service import get_spot_arbitrage_opportunities
    return await get_spot_arbitrage_opportunities()


@router.get("/dex-short", dependencies=[Depends(_enforce_screener_rl)])
async def dex_short_opportunities():
    """DEX-short cash-and-carry: buy spot on a DEX (via DexScreener), short perp on CEX.
    Canonical endpoint."""
    from backend.services.dex_arbitrage_service import get_dex_arbitrage_opportunities
    return await get_dex_arbitrage_opportunities()


@router.get("/dex-arbitrage", deprecated=True, dependencies=[Depends(_enforce_screener_rl)])
async def dex_arbitrage_opportunities():
    """Legacy alias for /dex-short."""
    from backend.services.dex_arbitrage_service import get_dex_arbitrage_opportunities
    return await get_dex_arbitrage_opportunities()


@router.get("/dex-spot", dependencies=[Depends(_enforce_screener_rl)])
async def dex_spot_opportunities():
    """DEX↔CEX spot-only arbitrage. Both legs spot; no funding/perp.
    File produced by go-fetcher dex_spot compute when AVALANT_DEX_SPOT=1.
    When the flag is off the file doesn't exist → cold envelope returned."""
    from backend.services import arbitrage_service as _arb
    cached = await _arb._read_file_cache_async("dex_spot_arbitrage.json", max_age=120.0)
    if cached and isinstance(cached, dict) and cached.get("opportunities") is not None:
        return _arb._apply_admin_filters(cached)
    # Cold path: same 500 ms polling pattern as /dex-short — Go writes
    # the file every 30s when enabled. If flag off, every attempt sees
    # ENOENT → cold envelope.
    for _ in range(10):
        await asyncio.sleep(0.05)
        cached = await _arb._read_file_cache_async("dex_spot_arbitrage.json", max_age=120.0)
        if cached and isinstance(cached, dict) and cached.get("opportunities") is not None:
            return _arb._apply_admin_filters(cached)
    return {"opportunities": [], "generated_at": int(time.time()),
            "symbols_scanned": 0, "cex_hits": 0, "cex_exchanges": [], "cold": True}


@router.get("/all-arbitrage", dependencies=[Depends(_enforce_screener_rl)])
async def all_arbitrage():
    """Combined feed of every arb mode the screener exposes: futures
    L/S, spot-short, DEX-short, AND dex-spot. Funding-arb is a derived
    view of spot-short (positive-funding subset), so it's covered by
    `spot_opps` already. Funding-rates is informational, not arbitrage,
    so it's intentionally excluded. Sorted by net profit, top 500."""
    from backend.services.spot_arbitrage_service import get_spot_arbitrage_opportunities as _spot
    from backend.services.dex_arbitrage_service import get_dex_arbitrage_opportunities as _dex
    from backend.services import arbitrage_service as _arb

    async def _read_dex_spot():
        cached = await _arb._read_file_cache_async("dex_spot_arbitrage.json", max_age=120.0)
        if cached and isinstance(cached, dict):
            return cached
        return {"opportunities": []}

    fut, spot, dex, dex_spot = await asyncio.gather(
        get_arbitrage_opportunities(),
        _spot(),
        _dex(),
        _read_dex_spot(),
        return_exceptions=True,
    )
    fut_opps      = [] if isinstance(fut,      BaseException) else (fut.get("opportunities")      or [])
    spot_opps     = [] if isinstance(spot,     BaseException) else (spot.get("opportunities")     or [])
    dex_opps      = [] if isinstance(dex,      BaseException) else (dex.get("opportunities")      or [])
    dex_spot_opps = [] if isinstance(dex_spot, BaseException) else (dex_spot.get("opportunities") or [])
    for r in fut_opps:      r.setdefault("type", "futures")
    for r in dex_spot_opps: r.setdefault("type", "dex_spot")
    merged = list(fut_opps) + list(spot_opps) + list(dex_opps) + list(dex_spot_opps)
    merged.sort(key=lambda x: x.get("net_profit", 0.0), reverse=True)
    return {
        "opportunities": merged[:500],
        "generated_at": int(time.time()),
        "counts": {
            "futures":    len(fut_opps),
            "spot_short": len(spot_opps),
            "dex_short":  len(dex_opps),
            "dex_spot":   len(dex_spot_opps),
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
    # get_exchange_health() reads funding_ws.json + funding.json synchronously
    # (~1MB parse). Offload to a thread so we don't stall the event loop for
    # other concurrent requests on this worker.
    data = await asyncio.to_thread(get_exchange_health)
    return {"exchanges": data, "generated_at": time.time()}


@router.get("/availability")
async def availability():
    """Tiny payload: enabled exchanges + all current funding symbols. Used by
    the /arb pre-flight to confirm the selected exchange/symbol aren't admin-
    disabled. Served from the shared funding.json file cache so every
    worker returns in <30ms without any upstream refetch."""
    from backend.services import admin_settings
    from backend.services.arbitrage_service import _read_file_cache_async

    now = time.time()
    if _availability_cache["data"] and now - _availability_cache["ts"] < _AVAILABILITY_TTL:
        return _availability_cache["data"]

    # Shared file cache — written by the broadcaster every 3s. Tolerate up
    # to 30s stale; the user-visible cost of a slightly stale symbol list is
    # zero (we just show the page).
    data = await _read_file_cache_async("funding.json", max_age=30.0)
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
    symbol: str = Query(..., pattern=r"^[A-Za-z0-9]{1,24}$"),
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
        # APR is funding-only — no one-shot entry-basis pickup, just the
        # sustainable annual rate. Net/8h still includes spread (or in_pct
        # when the frontend has a fresh orderbook tick) for the entry view.
        net_funding_only = gross - total_fees
        return {"source": "rates", "opp": {
            "symbol": sym, "long_exchange": long_ex, "short_exchange": short_ex,
            "long_rate": round(rate_l * 100, 6), "short_rate": round(rate_s * 100, 6),
            "long_price": p_l, "short_price": p_s,
            # /pair was hardcoded to 0 for both volume sides — front-end
            # accordingly showed "$0" Vol on every direct-pair lookup
            # even when the underlying rate cache had the real number.
            "long_volume": float(r_long.get("volume_usd") or 0),
            "short_volume": float(r_short.get("volume_usd") or 0),
            "long_interval_h": r_long.get("interval_h", 8),
            "short_interval_h": r_short.get("interval_h", 8),
            # Next funding timestamp — Unix seconds. UI on /arb shows
            # countdown until next payout for each leg. Was missing
            # entirely (handler didn't forward), so the page always
            # rendered "—" under NEXT.
            "long_next_ts": r_long.get("next_ts"),
            "short_next_ts": r_short.get("next_ts"),
            "gross_funding": round(gross * 100, 6),
            "price_spread": round(spread * 100, 4),
            "fee_long": round(fee_l * 100, 4), "fee_short": round(fee_s * 100, 4),
            "total_fees": round(total_fees * 100, 4),
            "net_profit": round(net * 100, 6),
            "gross_apr": round(gross * (8760/8) * 100, 4),
            "net_apr": round(net_funding_only * (8760/8) * 100, 4),
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
    "paradex","extended","htx","kraken","backpack","lighter",
    # Spot venues — orderbook_cache has REST fallback + WS adapters for
    # all of these. Routing through the same /orderbook endpoint keeps
    # callers (frontend WS pair keys, /orderbooks batch) on a single
    # path and lets the WS adapter populate books.json.
    "binance_spot","bybit_spot","okx_spot","gate_spot","kucoin_spot",
    "bitget_spot","bingx_spot","htx_spot","mexc_spot",
}

_SPOT_OB_EX = {"binance","bybit","okx","gate","kucoin","mexc","bitget","bingx","htx"}


@router.get("/orderbook-spot")
async def get_spot_orderbook(
    symbol: str = Query(..., pattern=r"^[A-Za-z0-9]{1,24}$"),
    exchange: str = Query(..., min_length=2, max_length=24),
    # Match /orderbook's le=500 — frontend always polls limit=200 and was
    # 422'ing every spot ladder (mexc/bybit/htx/...) on the validation gate.
    # The orderbook_cache layer normalises per-venue (5/20/100 etc) anyway.
    limit: int = Query(200, ge=1, le=500),
):
    """Spot orderbook for a CEX venue. Routes through the cache + WS
    pipeline (key '<ex>_spot') so subsequent /ws/book subscribers see live
    deltas, and so my REST fallback in orderbook_cache._fetch_direct_raw
    kicks in when the WS dropouts. Returns the same shape as /orderbook."""
    from fastapi import HTTPException
    from backend.services.orderbook_cache import get_cached_orderbook

    ex = exchange.lower()
    sym = symbol.upper()
    if ex not in _SPOT_OB_EX:
        raise HTTPException(400, f"unsupported exchange for spot orderbook: {ex}")
    return await get_cached_orderbook(f"{ex}_spot", sym, limit)


@router.get("/orderbook")
async def get_orderbook(
    symbol: str = Query(..., pattern=r"^[A-Za-z0-9]{1,24}$"),
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
    limit: int = Query(20, ge=1, le=500),
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


class _InOutBody(BaseModel):
    items: list[str] = Field(default_factory=list, max_length=512)


@router.post("/in-out")
async def in_out_basis_post(body: _InOutBody):
    """POST variant of /in-out — body carries the items list as JSON.
    Used by the screener because nginx caps query strings at ~8 KB and
    256-key batches push past that with their type:sym:long:short shape
    (≈40 chars each + URL-encoded colons → ~12 KB)."""
    return await _in_out_resolve(",".join(body.items))


@router.get("/in-out")
async def in_out_basis(
    items: str = Query(..., max_length=8192,
                       description="Comma-separated keys: type:SYM:longEx:shortEx — type=futures|spot|dex"),
):
    """GET variant — kept for small ad-hoc tests. Frontend uses POST."""
    return await _in_out_resolve(items)


async def _in_out_resolve(items: str):
    """Per-row live entry/exit basis for the screener tables.

    For each row computes:
        in_pct  = (bestBidShort - bestAskLong)  / bestAskLong  * 100
        out_pct = (bestBidLong  - bestAskShort) / bestAskShort * 100

    Routing:
        type=futures - long+short via /orderbook (perp)
        type=spot    - long via <ex>_spot, short via perp
        type=dex     - long is dex_arbitrage.json dex_price (single, no orderbook),
                       short via perp orderbook

    Top-of-book only — top-of-book is the right metric for screener; /arb has
    the size-aware variant via sampleEntryExit().

    Hot-path policy: this endpoint NEVER falls through to REST or triggers WS
    subscribe. It only reads in-memory book caches: Redis (`ob:<ex>:<sym>`)
    and the books.<ex>.json file memo. Anything that's not in those returns
    `null` for that row, which the frontend renders as "—". The reason is
    latency — the screener fires this every 3s for up to 64 rows; with REST
    fallback in the loop a single uncached pair was costing 200-500ms and
    starving the FastAPI worker pool. Cold rows are rare (top-vol pairs
    are always WS-subscribed) and the user's perception is "live" the moment
    a tick lands, not a perfect reading on the first poll after page load.
    """
    # Direct file-memo access — _file_lookup() can't be used here because
    # its freshness check compares `time.time()` (seconds) against the go-
    # fetcher-written `ts` field (milliseconds), so it always rejects every
    # entry as stale. We also can't go through `entry.get("data")` because
    # the per-venue and merged JSON files store bids/asks at the top level,
    # not under a "data" wrapper. The books.<ex>.json files are written
    # every 100ms by the dumper so any entry we read is fresh-enough by
    # construction. Bypass the broken helpers and read the in-memory memo
    # the orderbook_cache populates.
    from backend.services import orderbook_cache as _obc
    try:
        from backend.services.orderbook_redis import read_books_batch
    except Exception:  # noqa: BLE001
        read_books_batch = None

    parsed: list[tuple[str, str, str, str]] = []
    # Cap bumped 64 -> 256 because the screener now ranks the top
    # 1000 by basis and prefers to keep In/Out visible for as many of
    # those as books are subscribed for. The frontend rotates through
    # the full set in chunks of 256 so the user-touch path warms all
    # of them within ~12s of page load.
    for raw in items.split(",")[:256]:  # cap
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(":")
        if len(parts) != 4:
            continue
        typ, sym, le, se = (parts[0].lower(), parts[1].upper(),
                            parts[2].lower(), parts[3].lower())
        if typ not in ("futures", "spot", "dex"):
            continue
        if le and le not in _ORDERBOOK_EX and le not in _SPOT_OB_EX and typ != "dex":
            continue
        if se not in _ORDERBOOK_EX:
            continue
        parsed.append((typ, sym, le, se))

    if not parsed:
        return {}

    # Build orderbook fetch list. dex rows only need short side.
    fetch_keys: set[tuple[str, str]] = set()
    for typ, sym, le, se in parsed:
        fetch_keys.add((se, sym))
        if typ == "futures":
            fetch_keys.add((le, sym))
        elif typ == "spot":
            fetch_keys.add((f"{le}_spot", sym))

    # Single Redis MGET for every (ex, sym) — one round-trip total instead
    # of N sequential GETs. The 64-row screener page shrinks from ~700 ms
    # of accumulated Redis RTT to a single ~5 ms MGET.
    redis_hits: dict[str, dict] = {}
    if read_books_batch is not None:
        pair_strings = [f"{ex}:{sym}" for (ex, sym) in fetch_keys]
        try:
            raw = read_books_batch(pair_strings)
            for k, v in raw.items():
                d = (v or {}).get("data") or {}
                if d.get("bids") or d.get("asks"):
                    redis_hits[k] = d
        except Exception:  # noqa: BLE001
            pass

    # Fall through to the in-memory book memo for pairs Redis didn't have.
    # Refresh the memo at most once per call (it self-throttles inside).
    _obc._refresh_file_memo()
    file_memo = _obc._file_memo

    book_by: dict[tuple[str, str], dict] = {}
    for ex, sym in fetch_keys:
        pair = f"{ex}:{sym}"
        if pair in redis_hits:
            book_by[(ex, sym)] = redis_hits[pair]
            continue
        fd = file_memo.get(pair)
        if fd and (fd.get("bids") or fd.get("asks")):
            book_by[(ex, sym)] = fd
        else:
            book_by[(ex, sym)] = {"bids": [], "asks": []}

    # touch_user_sub on misses removed: it writes user_subs.json on
    # every call (atomic-rename pattern → 1 read + 1 write per call).
    # With 10 /in-out calls/sec × 30 misses each, that was 300 file
    # writes/sec and was driving 11 %+ iowait + saturating both Python
    # workers. Phase B prewarm (PrewarmFromArbFiles in go-fetcher,
    # every 60 s) covers the warming path: pairs in the top-1000
    # tracked set get books subscribed automatically. Misses past
    # that set just stay null — they're outside the screener's
    # actionable window anyway.

    # DEX prices read from dex_arbitrage.json — already loaded periodically.
    dex_px_by: dict[str, float] = {}
    if any(t == "dex" for t, *_ in parsed):
        try:
            from backend.services.dex_arbitrage_service import (
                get_dex_arbitrage_opportunities,
            )
            dex_data = await get_dex_arbitrage_opportunities()
            for opp in (dex_data or {}).get("opportunities", []):
                s = opp.get("symbol")
                px = opp.get("dex_price")
                if s and px:
                    dex_px_by[s] = float(px)
        except Exception:  # noqa: BLE001
            pass

    def _top(book: dict, side: str) -> float:
        levels = book.get(side) or []
        if not levels:
            return 0.0
        try:
            return float(levels[0][0])
        except (TypeError, ValueError, IndexError):
            return 0.0

    out: dict[str, dict] = {}
    for typ, sym, le, se in parsed:
        key = f"{typ}:{sym}:{le}:{se}"
        s_book = book_by.get((se, sym), {"bids": [], "asks": []})
        bestBidShort = _top(s_book, "bids")
        bestAskShort = _top(s_book, "asks")

        if typ == "dex":
            dex_px = dex_px_by.get(sym, 0.0)
            if dex_px <= 0 or bestBidShort <= 0 or bestAskShort <= 0:
                out[key] = {"in": None, "out": None}
                continue
            in_pct = (bestBidShort - dex_px) / dex_px * 100.0
            out_pct = (dex_px - bestAskShort) / bestAskShort * 100.0
            out[key] = {"in": round(in_pct, 4), "out": round(out_pct, 4)}
            continue

        l_book = book_by.get(
            (le if typ == "futures" else f"{le}_spot", sym),
            {"bids": [], "asks": []},
        )
        bestAskLong = _top(l_book, "asks")
        bestBidLong = _top(l_book, "bids")
        if bestAskLong <= 0 or bestBidLong <= 0 or bestAskShort <= 0 or bestBidShort <= 0:
            out[key] = {"in": None, "out": None}
            continue
        in_pct = (bestBidShort - bestAskLong) / bestAskLong * 100.0
        out_pct = (bestBidLong - bestAskShort) / bestAskShort * 100.0
        out[key] = {"in": round(in_pct, 4), "out": round(out_pct, 4)}

    # One-line summary per call so we can see hit/miss split. Logged
    # via the existing `avalant.screener` logger which is wired into
    # the role's stdout. Always-on; this is a small string per call.
    try:
        n_resolved = sum(1 for v in out.values() if v.get("in") is not None)
        n_null     = len(out) - n_resolved
        sample_misses = [k for k, v in out.items() if v.get("in") is None][:3]
        logger.info(
            "in-out: req=%d redis=%d ok=%d null=%d misses=%s",
            len(parsed), len(redis_hits), n_resolved, n_null,
            sample_misses,
        )
    except Exception:  # noqa: BLE001
        pass

    return out


@router.get("/arb-spread-history")
async def arb_spread_history(
    symbol: str = Query(..., pattern=r"^[A-Za-z0-9._-]{1,32}$"),
    long: str = Query(..., pattern=r"^[a-z0-9_-]{1,16}$"),
    short: str = Query(..., pattern=r"^[a-z0-9_-]{1,16}$"),
    tf: str = Query("auto", pattern=r"^(auto|5s|1m|1h)$"),
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
):
    """OHLC time-series of in/out spread for a (long, short, symbol) tuple.

    Three storage tiers chosen by `tf`:
      - `5s` — last 24h, 5-second buckets
      - `1m` — last 7d, 1-minute buckets
      - `1h` — last 90d, 1-hour buckets
      - `auto` — server picks by span size, capped at 1500 candles in
        the response so neither client nor chart engine drown on huge
        ranges.

    Returns `{tf, candles: [{t, in_o, in_h, in_l, in_c, out_o, ..., n}]}`.
    The chart treats absent buckets as whitespace (LWC `whitespace_data`)
    so gaps where go-fetcher was down don't draw a fake connected line.
    """
    import time as _t
    from sqlalchemy import text
    from backend.db.base import SessionLocal

    now = int(_t.time())
    # Default window: 30 minutes back from now — typical first paint
    # for the entry/exit chart on /arb.
    if to_ts is None:
        to_ts = now
    if from_ts is None:
        from_ts = to_ts - 30 * 60
    if from_ts >= to_ts:
        return {"tf": tf if tf != "auto" else "5s", "candles": []}
    span_s = to_ts - from_ts

    # tf=auto: pick the coarsest tier that still gives reasonable
    # density. Goal: ≥30 candles for context, ≤1500 to keep the chart
    # responsive. Boundaries match the per-tier retention so the picked
    # tier always has data for `from_ts`.
    chosen_tf = tf
    if tf == "auto":
        if span_s <= 5 * 60:        # ≤5m → 5s (≤60 candles)
            chosen_tf = "5s"
        elif span_s <= 2 * 3600:    # ≤2h → 1m (≤120 candles)
            chosen_tf = "1m"
        else:                        # >2h → 1h
            chosen_tf = "1h"
    # If the requested tf can't cover `from_ts` (retention window
    # crossed), bump to a coarser tier rather than serving partial data.
    max_lookback = {"5s": 24*3600, "1m": 7*24*3600, "1h": 90*24*3600}
    if now - from_ts > max_lookback[chosen_tf]:
        if chosen_tf == "5s":
            chosen_tf = "1m"
        if now - from_ts > max_lookback[chosen_tf] and chosen_tf == "1m":
            chosen_tf = "1h"

    table = {"5s": "arb_spread_candles_5s",
             "1m": "arb_spread_candles_1m",
             "1h": "arb_spread_candles_1h"}[chosen_tf]

    # Cap candles at 1500. If the natural query would return more,
    # widen the tier rather than truncate — truncation hides recent
    # data which is what the user is actually looking at.
    bucket_secs = {"5s": 5, "1m": 60, "1h": 3600}[chosen_tf]
    estimated = span_s // bucket_secs
    if estimated > 1500:
        # One more tier bump.
        chosen_tf = {"5s": "1m", "1m": "1h", "1h": "1h"}[chosen_tf]
        table = {"5s": "arb_spread_candles_5s",
                 "1m": "arb_spread_candles_1m",
                 "1h": "arb_spread_candles_1h"}[chosen_tf]

    db = SessionLocal()
    try:
        rows = db.execute(text(f"""
            SELECT bucket_ts, in_open, in_high, in_low, in_close,
                   out_open, out_high, out_low, out_close, samples
            FROM {table}
            WHERE exchange_long = :el AND exchange_short = :es
              AND symbol = :sym
              AND bucket_ts >= :from_ts AND bucket_ts <= :to_ts
            ORDER BY bucket_ts ASC
            LIMIT 1500
        """), {
            "el": long.lower(), "es": short.lower(),
            "sym": symbol.upper(),
            "from_ts": from_ts, "to_ts": to_ts,
        }).all()
    finally:
        db.close()

    candles = [{
        "t":     int(r[0]),
        "in_o":  float(r[1]), "in_h": float(r[2]),
        "in_l":  float(r[3]), "in_c": float(r[4]),
        "out_o": float(r[5]), "out_h": float(r[6]),
        "out_l": float(r[7]), "out_c": float(r[8]),
        "n":     int(r[9]),
    } for r in rows]
    return {"tf": chosen_tf, "candles": candles}


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
                            ctx = ctxs[i]
                            oi = float(ctx.get("openInterest", 0))
                            mark = float(ctx.get("markPx", 0))
                            oi_usd = oi * mark if mark else 0
                            return {"exchange": exchange, "oi": oi, "oi_usd": oi_usd, "unit": "contracts"}
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

# Per-user WS channel for position / trigger state changes. Trigger service
# and reconcile push tiny "refresh" events here — the client refetches data
# via REST so we don't have to serialize full state into the WS payload.
# Keyed by user_id, value is a set of active WebSocket connections for that
# user (multi-tab support).
_position_clients: dict[int, set[WebSocket]] = {}


def notify_position_update(user_id: int, kind: str = "refresh", payload: dict | None = None) -> None:
    """Fire-and-forget push to all active /ws/positions clients for `user_id`.

    Called from trigger_order_service, reconcile_service, and the arb-orders
    API after any state change. The WS message is intentionally tiny — clients
    refetch via REST. This keeps the wire format trivial and avoids embedding
    serialization knowledge into half a dozen call sites.

    Safe to call from sync code; we schedule the actual send on the running
    event loop. If no loop is running (rare — happens during shutdown), the
    notification is silently dropped.
    """
    clients = _position_clients.get(user_id)
    if not clients:
        return
    msg = json.dumps({"type": kind, **(payload or {})})
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    snapshot = list(clients)

    async def _send():
        results = await asyncio.gather(
            *(ws.send_text(msg) for ws in snapshot),
            return_exceptions=True,
        )
        dead = {ws for ws, r in zip(snapshot, results) if isinstance(r, Exception)}
        if dead:
            for ws in dead:
                clients.discard(ws)
    try:
        loop.create_task(_send())
    except RuntimeError:
        pass
# Push to connected WS clients every 1s. We already use diff payloads on
# /ws/arb so the wire cost of this is ~3-10KB per tick (only changed rows).
# /ws/funding sends a full snapshot; each push is ~300KB but gzip-compressed
# it's <100KB and every client handles that in <50ms.
BROADCAST_INTERVAL = _env_float("AVALANT_BROADCAST_INTERVAL", 0.25)
# WS push cadence: 250ms gives sub-second refresh without drowning clients.
# Funding is diff-payloaded so CPU cost per tick is bounded by row-change count;
# at steady-state only a handful of rows move per 250ms window.


async def _push(clients: set[WebSocket], msg: str) -> None:
    """Fan-out send. Sequential per-client await turned the broadcast loop
    into an N×latency bottleneck — 50 clients × 5 ms send = 250 ms, exactly
    one BROADCAST_INTERVAL, so the loop perpetually ran on its own tail.
    asyncio.gather fires all sends concurrently; per-client latency is now
    bounded by the slowest client, not the sum.
    """
    snapshot = list(clients)
    if not snapshot:
        return
    results = await asyncio.gather(
        *(ws.send_text(msg) for ws in snapshot),
        return_exceptions=True,
    )
    dead = {ws for ws, r in zip(snapshot, results) if isinstance(r, Exception)}
    if dead:
        clients -= dead
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

    # Throttle for the freshness-keeping snapshot we write to funding.json
    # below. Web role reads `ts` off this file for the Exchange-status
    # strip; if we only wrote inside `get_funding_data`, the file would
    # only be touched when a REST gather completed (10-30 s). Keeping a
    # 2 s heartbeat there means the freshness number stays close to live.
    _LAST_HEARTBEAT_WRITE = 0.0
    _HEARTBEAT_INTERVAL = 1.0  # write rate ceiling — file lives on tmpfs, cheap

    # Pull live WS rows directly so _cache.ts tracks the WS push, not just
    # the last successful REST gather. Without this the heartbeat below
    # writes ts_by_ex from REST timestamps only, which can be 10-30s old
    # on Contabo's degraded path even though WS is delivering sub-second.
    try:
        from backend.services.funding_ws import get_ws_rows as _get_ws_rows
    except Exception:
        _get_ws_rows = None
    # In multiprocess fetcher mode each WS adapter runs in its own
    # subprocess and dumps to /tmp/avalant_cache/funding_ws.{ex}.json
    # — main-process get_ws_rows() returns 0 for those venues. Read the
    # per-exchange dump as a second source of truth.
    _CACHE_DIR = _os.environ.get("AVALANT_CACHE_DIR", "/tmp/avalant_cache")

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

            # For exchanges absent from the merged file (race: Python last wrote
            # without them), fall back to Go's per-exchange files directly.
            # These are written by go-fetcher independently and are always current.
            for _ex in FETCHERS:
                if _ex in shared_rows_by_ex:
                    continue
                if (_cache.get(_ex) or ([], 0.0))[0]:  # Python REST cache is warm
                    continue
                _go_file = _os.path.join(_CACHE_DIR, f"funding.{_ex}.json")
                try:
                    with open(_go_file, "rb") as _gf:
                        _go_data = json.loads(_gf.read())
                    if not isinstance(_go_data, dict):
                        continue
                    _go_rows: list[dict] = []
                    for _sym, _tick in _go_data.items():
                        if not isinstance(_tick, dict):
                            continue
                        _rate = _tick.get("rate") or 0
                        _price = _tick.get("mark_price") or 0
                        if not _rate or not _price:
                            continue
                        _ivl = float(_tick.get("interval_h") or 1.0)
                        _nf_ms = int(_tick.get("next_funding") or 0)
                        _apr = round(_rate * (8760.0 / _ivl) * 100, 4) if _ivl else None
                        _go_rows.append({
                            "symbol": _sym,
                            "exchange": _ex,
                            "rate": _rate,
                            "price": _price,
                            "volume_usd": _tick.get("volume_24h") or 0,
                            "interval_h": _ivl,
                            "next_ts": _nf_ms // 1000 if _nf_ms else 0,
                            "apr": _apr,
                        })
                    if _go_rows:
                        shared_rows_by_ex[_ex] = _go_rows
                except Exception:
                    pass

            # Pre-pull WS rows for every venue so _cache stays current with
            # the live push. Two sources, in priority order:
            #   1. funding_ws/manager.get_ws_rows() — works for adapters
            #      running in the main process (paradex).
            #   2. /tmp/avalant_cache/funding_ws.{ex}.json — written by
            #      multiprocess WS workers (binance/bybit/mexc/etc.).
            # If either returns rows, stamp _cache[ex] with `now_m` so the
            # heartbeat below records a per-venue ts that tracks the WS
            # push, not the (possibly minutes-old) REST gather.
            now_m = _mono()
            now_t = time.time()
            from backend.services.arbitrage_service import _cache as _c
            for ex in FETCHERS:
                ws_rows = []
                # Source 1: in-process manager
                if _get_ws_rows is not None:
                    try:
                        ws_rows = _get_ws_rows(ex) or []
                    except Exception:
                        ws_rows = []
                # Source 2: per-exchange dump from subprocess WS worker
                if not ws_rows:
                    dump_rows, dump_ts = _read_ws_dump_for(ex, _CACHE_DIR)
                    if dump_rows and (now_t - dump_ts) < 30.0:
                        ws_rows = dump_rows
                if not ws_rows:
                    continue
                normalised = []
                for r in ws_rows:
                    if r.get("interval_h") is None:
                        continue
                    rr = dict(r)
                    rr["exchange"] = ex
                    rr.setdefault("volume_usd", 0)
                    rr.setdefault("next_ts", 0)
                    normalised.append(rr)
                if normalised:
                    _c[ex] = (normalised, now_m)

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

            # Heartbeat write to funding.json — kept BEFORE the subprocess-mode
            # short-circuit below so freshness stays close to real-time even
            # when arb compute is delegated. Web-role replicas read this file's
            # `ts` (and `ts_by_ex`) for the Exchange-status strip.
            now_t = time.time()
            # Write the heartbeat even when `rows` is briefly empty so the
            # file's mtime tracks "refresh-loop is alive" — venues without
            # per-exchange WS dumps (htx/extended/ethereal/paradex) read
            # their freshness off this file, and a 10s gap shows up as a
            # stale row on the dashboard.
            if now_t - _LAST_HEARTBEAT_WRITE >= _HEARTBEAT_INTERVAL:
                _LAST_HEARTBEAT_WRITE = now_t
                ex_set = set()
                for r in rows:
                    if r.get("exchange"):
                        ex_set.add(r["exchange"])
                # Per-venue freshness: prefer the WS subprocess's own ts dump
                # (wall-clock, no monotonic→wall conversion needed). Fall back
                # to the in-memory _cache stamp the pre-pull above sets.
                ts_by_ex = {}
                # Also pull paradex from the shared funding_ws.json (it's the
                # one venue that runs in the main process WS manager rather
                # than a subprocess, so it writes there, not to per-ex file).
                try:
                    with open(f"{_CACHE_DIR}/funding_ws.json", "rb") as _ff:
                        _shared_ws = json.loads(_ff.read())
                    _shared_tbe = _shared_ws.get("ts_by_ex") or {}
                except Exception:
                    _shared_tbe = {}
                for ex in FETCHERS:
                    if ex in disabled_ex:
                        continue
                    # Source 1: per-exchange WS subprocess dump
                    _rows_ws, _ws_ts = _read_ws_dump_for(ex, _CACHE_DIR)
                    if _ws_ts and (now_t - _ws_ts) < 30.0:
                        ts_by_ex[ex] = _ws_ts
                        continue
                    # Source 2: shared WS file (paradex)
                    _shared_ts = _shared_tbe.get(ex)
                    if _shared_ts and (now_t - float(_shared_ts)) < 30.0:
                        ts_by_ex[ex] = float(_shared_ts)
                        continue
                    # Source 3: in-memory _cache stamped by REST gather
                    _, cached_ts = _cache.get(ex, ([], 0.0))
                    if cached_ts:
                        ts_by_ex[ex] = now_t - (now_m - cached_ts)
                await _write_file_cache_async("funding.json", {
                    "ts": int(now_t),
                    "exchanges": sorted(ex_set),
                    "rows": rows,
                    "ts_by_ex": ts_by_ex,
                })

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


async def _ws_authenticate(websocket: WebSocket, label: str, *, required: bool = False) -> int | None:
    """Auth via first-frame {"auth": "<JWT>"} after accept().

    The JWT used to be passed as ?token= in the URL — that put it into nginx
    access logs (token leak). First-frame auth keeps the token in the WS
    payload only. 5 s wait window.

    Modes:
      · required=False (default for the public screener feeds): an empty /
        missing / invalid token is treated as anonymous (returns 0). The
        feed is identical for every connection — no per-user filtering —
        so anon clients on /screener get the same live data, with a
        2-minute soft gate on the page itself via /anon-gate.js.
      · required=True (for endpoints that DO need a user, e.g. /ws/book):
        any auth failure closes the socket with 4401.
    """
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        if required:
            try: await websocket.close(code=4401, reason="auth timeout")
            except Exception: pass
            return None
        return 0
    token = ""
    try:
        msg = json.loads(raw)
        if isinstance(msg, dict):
            token = str(msg.get("auth") or "").strip()
    except (ValueError, TypeError):
        pass
    if not token:
        if required:
            try: await websocket.close(code=4401, reason="auth required")
            except Exception: pass
            logger.debug("%s WS rejected — no auth frame", label)
            return None
        return 0
    user_id = decode_token(token)
    if not user_id:
        if required:
            try: await websocket.close(code=4401, reason="invalid token")
            except Exception: pass
            logger.debug("%s WS rejected — invalid token", label)
            return None
        return 0
    return user_id


async def _ws_handler(websocket: WebSocket, clients: set[WebSocket],
                      fetch_fn, label: str,
                      snapshot_builder=None) -> None:
    await websocket.accept()
    # Public live feeds — anon clients get the same data. Auth is read but
    # non-fatal; a returning user with a token shows up in logs by uid, an
    # anonymous viewer logs as uid=0.
    user_id = await _ws_authenticate(websocket, label, required=False)
    if user_id is None:
        return  # only happens if `required` becomes True someday
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


@router.websocket("/ws/positions")
async def positions_ws(websocket: WebSocket) -> None:
    """Per-user WS channel for arb-position / trigger state changes.

    Auth is required: anon clients have nothing to subscribe to. The wire
    payload is tiny — `{"type": "refresh"}` events when something changes.
    Client refetches /api/trade/arb-orders + /api/trade/arb-positions via
    REST. This keeps server-side logic simple and avoids stale-state
    serialization bugs.
    """
    await websocket.accept()
    user_id = await _ws_authenticate(websocket, "positions", required=True)
    if user_id is None or user_id == 0:
        return
    clients = _position_clients.setdefault(user_id, set())
    clients.add(websocket)
    logger.debug("positions WS connect uid=%s (per-user=%d, total=%d)",
                 user_id, len(clients), sum(len(c) for c in _position_clients.values()))
    try:
        # Send a hello so the client knows the connection is live.
        await websocket.send_json({"type": "hello"})
        while True:
            text = await websocket.receive_text()
            if text == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("positions WS error uid=%s: %s", user_id, exc)
    finally:
        clients.discard(websocket)
        if not clients:
            _position_clients.pop(user_id, None)


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
# 25ms cadence (down from 100ms) trims ~75ms off the worst-case
# orderbook → browser latency. Cost: 4× more diff-push CPU on the
# broadcast loop, but each tick is cheap (~1-2ms per client) and the
# broadcaster is single-process so the 4× is bounded by `len(clients)`,
# not by exchange count.
BOOK_BROADCAST_INTERVAL = _env_float("AVALANT_BOOK_BROADCAST_INTERVAL", 0.025)
BOOK_MAX_PAIRS_PER_CLIENT = 100  # /arb needs 2, /screener live In/Out needs ~80 for top-40 rows


async def _book_broadcast_loop() -> None:
    """Push fresh orderbook frames to subscribed clients.

    Read path: per-key Redis (`ob:<ex>:<sym>`) populated by the orderbook
    workers on every WS update (see WSManager._update_cb). Falls back to
    the file-based `_file_memo` only if Redis is unreachable. Reading per
    key from Redis means the broadcaster sees worker updates within the
    50 ms write throttle — vs the 100-230 ms master-merger tick of the
    file path. End-to-end orderbook → browser latency drops from ~250-
    400 ms to ~100-150 ms on the SG box.
    """
    from backend.services import orderbook_cache as _ob
    from backend.services.orderbook_redis import read_books_batch as _redis_batch_read
    while True:
        try:
            await asyncio.sleep(BOOK_BROADCAST_INTERVAL)
            if not _book_ws_subs:
                continue
            # Collect every unique pair across all subscribed clients so we
            # do exactly ONE Redis MGET per broadcast tick — instead of
            # N (clients × pairs) individual GET calls. This was the
            # dominant CPU cost on app/app2 once we moved to Redis reads.
            all_pairs: set[str] = set()
            for subs in _book_ws_subs.values():
                if subs:
                    all_pairs.update(subs.keys())
            redis_entries = _redis_batch_read(list(all_pairs)) if all_pairs else {}
            file_memo_refreshed = False
            # Build per-client payloads first, fire all sends concurrently.
            # Sequential await per-client made this loop scale O(clients ×
            # send_latency) per tick — at 25 ms BOOK_BROADCAST_INTERVAL and
            # 50 clients × 5 ms send, the loop ran on its own tail. Now total
            # tick time is bounded by the slowest single client.
            send_tasks = []
            for ws, subs in list(_book_ws_subs.items()):
                if not subs:
                    continue
                payload: dict[str, dict] = {}
                for pair, last_ts in list(subs.items()):
                    entry = redis_entries.get(pair)
                    if entry is None:
                        if not file_memo_refreshed:
                            _ob._refresh_file_memo()
                            file_memo_refreshed = True
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
                    send_tasks.append(ws.send_json({"books": payload}))
            if send_tasks:
                # Failures from dead clients drop here; the receive-loop side
                # cleans up the WS entry on the next iteration.
                await asyncio.gather(*send_tasks, return_exceptions=True)
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
    if not ex or not sym or len(ex) > 24 or len(sym) > 24:
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
