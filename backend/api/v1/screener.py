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
async def funding_rates(_=Depends(get_current_user)):
    """Funding rates across perpetual futures exchanges. Cached 30s per exchange."""
    return await get_funding_data()


@router.get("/arbitrage")
async def arbitrage_opportunities(_=Depends(get_current_user)):
    """Cross-exchange funding arbitrage opportunities with price spread and fees."""
    return await get_arbitrage_opportunities()


# ── Funding history per exchange/symbol ────────────────────────────────────────

async def _fetch_history_for(exchange: str, symbol: str, limit: int = 90) -> list[dict]:
    """Fetch historical funding rates for a symbol on a given exchange."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
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
        async with httpx.AsyncClient(timeout=10) as c:
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


@router.get("/orderbook")
async def get_orderbook(
    symbol: str = Query(...),
    exchange: str = Query(...),
    limit: int = Query(20),
    _=Depends(get_current_user),
):
    try:
        c = _arb_http  # reuse persistent client with keepalive
        if exchange == "binance":
            r = await c.get(f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}USDT&limit={limit}")
            d = r.json()
            return {"bids": [[float(x[0]), float(x[1])] for x in d["bids"]], "asks": [[float(x[0]), float(x[1])] for x in d["asks"]]}
        elif exchange == "bybit":
            r = await c.get(f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={symbol}USDT&limit={limit}")
            d = r.json().get("result", {})
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("b",[])], "asks": [[float(x[0]), float(x[1])] for x in d.get("a",[])]}
        elif exchange == "okx":
            r = await c.get(f"https://www.okx.com/api/v5/market/books?instId={symbol}-USDT-SWAP&sz={limit}")
            d = (r.json().get("data") or [{}])[0]
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids",[])], "asks": [[float(x[0]), float(x[1])] for x in d.get("asks",[])]}
        elif exchange == "gate":
            r = await c.get(f"https://api.gateio.ws/api/v4/futures/usdt/order_book?contract={symbol}_USDT&limit={limit}")
            d = r.json()
            return {"bids": [[float(x["p"]), float(x["s"])] for x in d.get("bids",[])], "asks": [[float(x["p"]), float(x["s"])] for x in d.get("asks",[])]}
        elif exchange == "kucoin":
            sym = symbol + "USDTM"
            r = await c.get(f"https://api-futures.kucoin.com/api/v1/level2/depth{limit}?symbol={sym}")
            d = r.json().get("data", {})
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids",[])], "asks": [[float(x[0]), float(x[1])] for x in d.get("asks",[])]}
        elif exchange == "mexc":
            r = await c.get(f"https://contract.mexc.com/api/v1/contract/depth/{symbol}_USDT?limit={limit}")
            d = r.json().get("data", {})
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids",[])], "asks": [[float(x[0]), float(x[1])] for x in d.get("asks",[])]}
        elif exchange == "bitget":
            r = await c.get(f"https://api.bitget.com/api/v2/mix/market/merge-depth?symbol={symbol}USDT&productType=USDT-FUTURES&limit={limit}")
            d = r.json().get("data", {})
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids",[])], "asks": [[float(x[0]), float(x[1])] for x in d.get("asks",[])]}
        elif exchange == "aster":
            r = await c.get(f"https://fapi.asterdex.com/fapi/v1/depth?symbol={symbol}USDT&limit={limit}")
            d = r.json()
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids",[])], "asks": [[float(x[0]), float(x[1])] for x in d.get("asks",[])]}
        elif exchange == "hyperliquid":
            r = await c.post("https://api.hyperliquid.xyz/info", json={"type":"l2Book","coin":symbol}, headers={"Content-Type":"application/json"})
            d = r.json().get("levels", [[],[]])
            return {"bids": [[float(x["px"]), float(x["sz"])] for x in d[0]], "asks": [[float(x["px"]), float(x["sz"])] for x in d[1]]}
        elif exchange == "bingx":
            r = await c.get(f"https://open-api.bingx.com/openApi/swap/v2/quote/depth?symbol={symbol}-USDT&limit={limit}")
            d = r.json().get("data", {})
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids",[])], "asks": [[float(x[0]), float(x[1])] for x in d.get("asks",[])]}
        elif exchange == "whitebit":
            r = await c.get(f"https://whitebit.com/api/v4/public/orderbook/{symbol}_PERP?limit={limit}&level=2")
            d = r.json()
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids",[])], "asks": [[float(x[0]), float(x[1])] for x in d.get("asks",[])]}
    except Exception as exc:
        logger.warning("Orderbook %s/%s failed: %s", exchange, symbol, exc)
    return {"bids": [], "asks": []}


@router.get("/arb-price-history")
async def arb_price_history(
    symbol: str = Query(...),
    long_ex: str = Query(...),
    short_ex: str = Query(...),
    _=Depends(get_current_user),
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
    _=Depends(get_current_user),
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
        async with httpx.AsyncClient(timeout=8) as c:
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
    except Exception as exc:
        logger.warning("OI %s/%s failed: %s", exchange, symbol, exc)
    return None


@router.get("/open-interest")
async def open_interest(
    symbol: str = Query(...),
    long_ex: str = Query(...),
    short_ex: str = Query(...),
    _=Depends(get_current_user),
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
    _=Depends(get_current_user),
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
BROADCAST_INTERVAL = 5  # seconds


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


async def _broadcast_loop() -> None:
    """Keep funding cache hot every BROADCAST_INTERVAL seconds."""
    # Kick off slow interval warmup in background — don't block the loop
    asyncio.create_task(_warmup())

    while True:
        await asyncio.sleep(BROADCAST_INTERVAL)
        try:
            # Always fetch — keeps HTTP cache hot even with no WS clients
            data = await get_funding_data()
            if _funding_clients:
                await _push(_funding_clients, json.dumps(data))
                logger.debug("Screener funding WS: pushed to %d clients", len(_funding_clients))
        except Exception as exc:
            logger.warning("Screener funding broadcast error: %s", exc)
        if _arb_clients:
            try:
                data = await get_arbitrage_opportunities()
                await _push(_arb_clients, json.dumps(data))
                logger.debug("Screener arb WS: pushed to %d clients", len(_arb_clients))
            except Exception as exc:
                logger.warning("Screener arb broadcast error: %s", exc)


def start_screener_broadcaster() -> None:
    global _broadcaster_task
    _broadcaster_task = asyncio.create_task(_broadcast_loop())
    logger.info("Screener broadcaster started")


def stop_screener_broadcaster() -> None:
    global _broadcaster_task
    if _broadcaster_task:
        _broadcaster_task.cancel()
        _broadcaster_task = None


async def _ws_handler(websocket: WebSocket, clients: set[WebSocket], token: str,
                      fetch_fn, label: str) -> None:
    user_id = decode_token(token)
    if not user_id:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    clients.add(websocket)
    logger.debug("Screener %s WS connect uid=%s (total=%d)", label, user_id, len(clients))

    try:
        data = await fetch_fn()
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
async def funding_ws(websocket: WebSocket, token: str = Query(...)) -> None:
    await _ws_handler(websocket, _funding_clients, token, get_funding_data, "funding")


@router.websocket("/ws/arb")
async def arb_ws(websocket: WebSocket, token: str = Query(...)) -> None:
    await _ws_handler(websocket, _arb_clients, token, get_arbitrage_opportunities, "arb")
