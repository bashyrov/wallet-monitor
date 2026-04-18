"""Hot orderbook cache with per-(exchange,symbol) background poller.

Motivation: the /arb page polls orderbook at 150ms per side. Without caching,
every tab fires N req/s per exchange — 4 uvicorn workers × M users × 2 sides.
Exchange rate limits kick in, latencies spike, UI shows "Collecting data…".

Design: one poller task per (exchange, symbol) runs in whichever worker saw
the request first. It fetches every POLL_INTERVAL and updates an in-memory
cache. Clients read the cache (~µs). After IDLE_TIMEOUT with no requests, the
poller exits. Each worker has its own cache, but load is O(unique pairs × workers)
instead of O(viewers × req/s).
"""
from __future__ import annotations

import asyncio
import logging
import time

from backend.services.arbitrage_service import _http as _arb_http

logger = logging.getLogger("avalant.orderbook")

POLL_INTERVAL   = 0.30   # exchange refresh cadence (seconds)
IDLE_TIMEOUT    = 30.0   # stop poller after this many seconds without a request
FIRST_WAIT      = 1.8    # cold-start: wait up to this long for initial data
STALE_FALLBACK  = 10.0   # still serve cached data if younger than this, even on error

_book_cache: dict[str, dict] = {}        # key → {"data": dict, "ts": float, "last_request": float}
_pollers: dict[str, asyncio.Task] = {}   # key → task
_lock = asyncio.Lock()


async def _fetch_direct(exchange: str, symbol: str, limit: int) -> dict | None:
    """Direct one-shot fetch from exchange. Returns {bids, asks} or None on failure."""
    c = _arb_http
    try:
        if exchange == "binance":
            r = await c.get(f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}USDT&limit={limit}")
            d = r.json()
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                    "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
        if exchange == "bybit":
            r = await c.get(f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={symbol}USDT&limit={limit}")
            d = r.json().get("result", {})
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("b", [])],
                    "asks": [[float(x[0]), float(x[1])] for x in d.get("a", [])]}
        if exchange == "okx":
            r = await c.get(f"https://www.okx.com/api/v5/market/books?instId={symbol}-USDT-SWAP&sz={limit}")
            d = (r.json().get("data") or [{}])[0]
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                    "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
        if exchange == "gate":
            r = await c.get(f"https://api.gateio.ws/api/v4/futures/usdt/order_book?contract={symbol}_USDT&limit={limit}")
            d = r.json()
            return {"bids": [[float(x["p"]), float(x["s"])] for x in d.get("bids", [])],
                    "asks": [[float(x["p"]), float(x["s"])] for x in d.get("asks", [])]}
        if exchange == "kucoin":
            sym = ("XBT" if symbol == "BTC" else symbol) + "USDTM"
            depth = 100 if limit > 20 else 20
            r = await c.get(f"https://api-futures.kucoin.com/api/v1/level2/depth{depth}?symbol={sym}")
            d = r.json().get("data", {})
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                    "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
        if exchange == "mexc":
            r = await c.get(f"https://contract.mexc.com/api/v1/contract/depth/{symbol}_USDT?limit={limit}")
            d = r.json().get("data", {})
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                    "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
        if exchange == "bitget":
            r = await c.get(f"https://api.bitget.com/api/v2/mix/market/merge-depth?symbol={symbol}USDT&productType=USDT-FUTURES&limit={limit}")
            d = r.json().get("data", {})
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                    "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
        if exchange == "aster":
            r = await c.get(f"https://fapi.asterdex.com/fapi/v1/depth?symbol={symbol}USDT&limit={limit}")
            d = r.json()
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                    "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
        if exchange == "hyperliquid":
            r = await c.post("https://api.hyperliquid.xyz/info",
                             json={"type": "l2Book", "coin": symbol},
                             headers={"Content-Type": "application/json"})
            d = r.json().get("levels", [[], []])
            return {"bids": [[float(x["px"]), float(x["sz"])] for x in d[0]],
                    "asks": [[float(x["px"]), float(x["sz"])] for x in d[1]]}
        if exchange == "bingx":
            r = await c.get(f"https://open-api.bingx.com/openApi/swap/v2/quote/depth?symbol={symbol}-USDT&limit={limit}")
            d = r.json().get("data", {})
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                    "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
        if exchange == "whitebit":
            r = await c.get(f"https://whitebit.com/api/v4/public/orderbook/{symbol}_PERP?limit={limit}&level=2")
            d = r.json()
            return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                    "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
    except Exception as exc:
        logger.debug("orderbook fetch %s/%s failed: %s", exchange, symbol, exc)
    return None


async def _poll_loop(key: str, exchange: str, symbol: str, limit: int) -> None:
    consecutive_fails = 0
    try:
        while True:
            entry = _book_cache.get(key)
            if not entry or time.time() - entry.get("last_request", 0) > IDLE_TIMEOUT:
                logger.info("orderbook poller idle, stopping: %s", key)
                return

            data = await _fetch_direct(exchange, symbol, limit)
            if data and (data.get("bids") or data.get("asks")):
                entry["data"] = data
                entry["ts"] = time.time()
                consecutive_fails = 0
            else:
                consecutive_fails += 1
                if consecutive_fails == 1 or consecutive_fails % 20 == 0:
                    logger.warning("orderbook poll empty/fail: %s (streak=%d)", key, consecutive_fails)
                if consecutive_fails >= 20:
                    await asyncio.sleep(3)  # backoff on persistent failure
                    continue

            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("orderbook poller crashed %s: %s", key, exc)
    finally:
        _pollers.pop(key, None)


async def get_cached_orderbook(exchange: str, symbol: str, limit: int = 50) -> dict:
    """Return {bids, asks}. Starts a background poller on first request per key.

    - First call (cold): waits up to FIRST_WAIT for initial data, then returns.
    - Subsequent calls: read from memory, typically <1ms.
    - If exchange errors but cached data < STALE_FALLBACK old, returns cached.
    """
    exchange = exchange.lower()
    symbol = symbol.upper()
    key = f"{exchange}:{symbol}"
    now = time.time()

    async with _lock:
        entry = _book_cache.setdefault(key, {})
        entry["last_request"] = now
        task = _pollers.get(key)
        if not task or task.done():
            _pollers[key] = asyncio.create_task(_poll_loop(key, exchange, symbol, limit))

    # Fast path: we already have data
    data = entry.get("data")
    ts = entry.get("ts", 0)
    if data and now - ts < STALE_FALLBACK:
        return data

    # Cold start: poll memory until first data arrives or deadline hits
    deadline = now + FIRST_WAIT
    while time.time() < deadline:
        await asyncio.sleep(0.05)
        entry = _book_cache.get(key) or {}
        data = entry.get("data")
        if data:
            return data

    return entry.get("data") or {"bids": [], "asks": []}


def cache_stats() -> dict:
    return {
        "pairs_cached": len(_book_cache),
        "active_pollers": sum(1 for t in _pollers.values() if not t.done()),
        "keys": list(_book_cache.keys()),
    }


def top_levels(exchange: str, symbol: str) -> tuple[float, float] | None:
    """Synchronous accessor: return (best_bid, best_ask) from cache, or None.
    Safe to call from threads — dict reads are atomic in CPython.
    """
    key = f"{exchange.lower()}:{symbol.upper()}"
    entry = _book_cache.get(key)
    if not entry:
        return None
    data = entry.get("data")
    if not data:
        return None
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    if not bids or not asks:
        return None
    try:
        return (float(bids[0][0]), float(asks[0][0]))
    except (ValueError, IndexError, TypeError):
        return None


async def prewarm(exchange: str, symbol: str, limit: int = 50) -> None:
    """Start/keep poller alive for this pair without waiting for data.
    Used by the top-arb prewarmer; fire-and-forget."""
    exchange = exchange.lower()
    symbol = symbol.upper()
    key = f"{exchange}:{symbol}"
    async with _lock:
        entry = _book_cache.setdefault(key, {})
        entry["last_request"] = time.time()
        task = _pollers.get(key)
        if not task or task.done():
            _pollers[key] = asyncio.create_task(_poll_loop(key, exchange, symbol, limit))


# ── Background prewarm: keep top arb pairs' books hot ─────────────────────────
PREWARM_INTERVAL = 15.0      # refresh the hot-set every 15s
PREWARM_TOP_N    = 80        # how many opportunities to keep warm
_prewarm_task: asyncio.Task | None = None


async def _prewarm_loop() -> None:
    from backend.services.arbitrage_service import get_arbitrage_opportunities
    logger.info("orderbook prewarm loop started (top=%d, interval=%.0fs)",
                PREWARM_TOP_N, PREWARM_INTERVAL)
    while True:
        try:
            data = await get_arbitrage_opportunities()
            opps = data.get("opportunities", [])[:PREWARM_TOP_N]
            seen: set[str] = set()
            for o in opps:
                for ex in (o.get("long_exchange"), o.get("short_exchange")):
                    if not ex:
                        continue
                    key = f"{ex}:{o['symbol']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    await prewarm(ex, o["symbol"])
            if opps:
                logger.debug("orderbook prewarm: %d pairs touched (%d opps)",
                             len(seen), len(opps))
        except Exception as exc:
            logger.warning("orderbook prewarm error: %s", exc)
        await asyncio.sleep(PREWARM_INTERVAL)


def start_prewarm() -> None:
    global _prewarm_task
    if _prewarm_task and not _prewarm_task.done():
        return
    _prewarm_task = asyncio.create_task(_prewarm_loop())


def stop_prewarm() -> None:
    global _prewarm_task
    if _prewarm_task:
        _prewarm_task.cancel()
        _prewarm_task = None
