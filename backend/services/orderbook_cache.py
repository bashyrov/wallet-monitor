"""Hot orderbook cache — one owner worker polls top-N arb pairs, all workers
read from a shared file cache.

Design:
  • A single uvicorn worker acquires /tmp/avalant_prewarm.lock and becomes the
    owner. It runs per-(exchange,symbol) pollers for the top-N arb opportunities
    (refreshed every 15s). Every 500ms it dumps all fresh books to a single
    JSON file under /tmp/avalant_cache/.
  • All workers (including the owner) serve reads:
        1. local in-memory _book_cache (fast — µs)
        2. file cache shared by owner (~ms, covers top-N)
        3. spawn a local poller (cold-start ~500ms) for pairs outside top-N
  • Result: load is O(top_N × 2) total req/s, NOT O(workers × users). Other
    workers do not poll unless a client asks for a non-top-N pair.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time

import httpx

# Dedicated httpx pool for orderbook polling so it doesn't compete with funding
# fetchers for connection slots.
_arb_http = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=3.0, read=4.0, write=3.0, pool=1.0),
    headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip, deflate"},
    follow_redirects=True,
    limits=httpx.Limits(max_connections=300, max_keepalive_connections=80, keepalive_expiry=30),
    http2=False,
)

logger = logging.getLogger("avalant.orderbook")

POLL_INTERVAL   = 0.50   # per-pair poll cadence (owner worker)
IDLE_TIMEOUT    = 30.0   # stop poller if no requests in this window
FIRST_WAIT      = 1.8    # max cold-start wait for first data
STALE_FALLBACK  = 10.0   # still serve local-cache data if younger than this
FILE_FRESH_MAX  = 5.0    # file-cache entry usable only if younger than this

_CACHE_DIR   = "/tmp/avalant_cache"
_BOOKS_FILE  = os.path.join(_CACHE_DIR, "books.json")
_LOCK_FILE   = "/tmp/avalant_prewarm.lock"

_book_cache: dict[str, dict] = {}        # key → {"data": dict, "ts": float, "last_request": float}
_pollers: dict[str, asyncio.Task] = {}
_lock = asyncio.Lock()

# Reader-side snapshot of shared file (refreshed on demand, throttled)
_file_memo: dict[str, dict] = {}
_file_memo_mtime: float = 0.0
_file_memo_last_check: float = 0.0
_FILE_CHECK_INTERVAL = 0.1  # re-open file at most every 100ms per worker


# ── Direct per-exchange fetch ────────────────────────────────────────────────
async def _fetch_direct(exchange: str, symbol: str, limit: int) -> dict | None:
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


# ── Poller task ──────────────────────────────────────────────────────────────
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
                    await asyncio.sleep(3)
                    continue
            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("orderbook poller crashed %s: %s", key, exc)
    finally:
        _pollers.pop(key, None)


# ── Shared file cache (cross-worker) ─────────────────────────────────────────
def _refresh_file_memo() -> None:
    """Throttled: re-read books.json at most every 100ms per worker."""
    global _file_memo, _file_memo_mtime, _file_memo_last_check
    now = time.time()
    if now - _file_memo_last_check < _FILE_CHECK_INTERVAL:
        return
    _file_memo_last_check = now
    try:
        st = os.stat(_BOOKS_FILE)
        if st.st_mtime == _file_memo_mtime:
            return
        with open(_BOOKS_FILE, "rb") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _file_memo = data
            _file_memo_mtime = st.st_mtime
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


def _file_lookup(key: str) -> dict | None:
    _refresh_file_memo()
    entry = _file_memo.get(key)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > FILE_FRESH_MAX:
        return None
    return entry.get("data")


# ── Public API ───────────────────────────────────────────────────────────────
async def get_cached_orderbook(exchange: str, symbol: str, limit: int = 50) -> dict:
    exchange = exchange.lower()
    symbol = symbol.upper()
    key = f"{exchange}:{symbol}"
    now = time.time()

    # 1. Local memory — fastest
    entry = _book_cache.get(key)
    if entry and entry.get("data") and now - entry.get("ts", 0) < STALE_FALLBACK:
        entry["last_request"] = now
        return entry["data"]

    # 2. Shared file cache — populated by owner worker for top-N pairs
    fd = _file_lookup(key)
    if fd:
        return fd

    # 3. Cold-start local poller (pair outside top-N, or owner warming up)
    async with _lock:
        entry = _book_cache.setdefault(key, {})
        entry["last_request"] = now
        task = _pollers.get(key)
        if not task or task.done():
            _pollers[key] = asyncio.create_task(_poll_loop(key, exchange, symbol, limit))

    deadline = now + FIRST_WAIT
    while time.time() < deadline:
        await asyncio.sleep(0.05)
        e = _book_cache.get(key) or {}
        if e.get("data"):
            return e["data"]
        fd = _file_lookup(key)
        if fd:
            return fd
    return (_book_cache.get(key) or {}).get("data") or {"bids": [], "asks": []}


def top_levels(exchange: str, symbol: str) -> tuple[float, float] | None:
    """Sync accessor (best_bid, best_ask). Checks local memory then file cache."""
    key = f"{exchange.lower()}:{symbol.upper()}"
    data: dict | None = None
    entry = _book_cache.get(key)
    if entry:
        data = entry.get("data")
    if not data:
        data = _file_lookup(key)
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


def cache_stats() -> dict:
    _refresh_file_memo()
    return {
        "local_pairs":   len(_book_cache),
        "active_pollers": sum(1 for t in _pollers.values() if not t.done()),
        "file_pairs":    len(_file_memo),
        "is_owner":      _prewarm_lock_fd is not None,
    }


# ── Prewarm owner loops (single worker) ──────────────────────────────────────
PREWARM_TOP_N        = 80
PREWARM_HOTLIST_S    = 15.0   # refresh hot list from arb result
PREWARM_DUMP_S       = 0.5    # snapshot to file

_prewarm_hotlist_task: asyncio.Task | None = None
_prewarm_dump_task:    asyncio.Task | None = None
_prewarm_lock_fd = None


async def _prewarm_start_poller(exchange: str, symbol: str, limit: int = 50) -> None:
    exchange = exchange.lower()
    symbol = symbol.upper()
    key = f"{exchange}:{symbol}"
    async with _lock:
        entry = _book_cache.setdefault(key, {})
        entry["last_request"] = time.time()
        task = _pollers.get(key)
        if not task or task.done():
            _pollers[key] = asyncio.create_task(_poll_loop(key, exchange, symbol, limit))


async def _prewarm_hotlist_loop() -> None:
    from backend.services.arbitrage_service import get_arbitrage_opportunities
    from backend.services.orderbook_ws import is_ws_supported, start_ws_manager
    while True:
        try:
            data = await get_arbitrage_opportunities()
            opps = data.get("opportunities", [])[:PREWARM_TOP_N]
            # Group pairs by exchange so each WS adapter gets one subscribe call
            ws_subs: dict[str, set[str]] = {}
            rest_pairs: set[tuple[str, str]] = set()
            for o in opps:
                sym = o["symbol"]
                for ex in (o.get("long_exchange"), o.get("short_exchange")):
                    if not ex:
                        continue
                    if is_ws_supported(ex):
                        ws_subs.setdefault(ex, set()).add(sym)
                    else:
                        rest_pairs.add((ex, sym))

            # WS: ensure each adapter is running with the current symbol set
            mgr = start_ws_manager()
            for ex, syms in ws_subs.items():
                mgr.subscribe(ex, list(syms))

            # REST: spawn pollers for exchanges without WS support (perp DEX + slow CEX)
            for ex, sym in rest_pairs:
                await _prewarm_start_poller(ex, sym)

            logger.info(
                "orderbook prewarm: ws=%s rest_pairs=%d (%d opps)",
                {ex: len(s) for ex, s in ws_subs.items()}, len(rest_pairs), len(opps),
            )
        except Exception as exc:
            logger.warning("prewarm hot-list error: %s", exc)
        await asyncio.sleep(PREWARM_HOTLIST_S)


async def _prewarm_dump_loop() -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    while True:
        try:
            cutoff = time.time() - FILE_FRESH_MAX
            snapshot: dict[str, dict] = {}
            for key, entry in list(_book_cache.items()):
                ts = entry.get("ts", 0)
                if ts < cutoff:
                    continue
                data = entry.get("data")
                if not data:
                    continue
                snapshot[key] = {"data": data, "ts": ts}
            tmp = _BOOKS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snapshot, f, separators=(",", ":"))
            os.replace(tmp, _BOOKS_FILE)
        except Exception as exc:
            logger.warning("prewarm dump error: %s", exc)
        await asyncio.sleep(PREWARM_DUMP_S)


def start_prewarm() -> None:
    """Attempt to become the prewarm owner. Only one worker wins the lock;
    others become passive file readers with no added polling load."""
    global _prewarm_hotlist_task, _prewarm_dump_task, _prewarm_lock_fd
    if _prewarm_hotlist_task and not _prewarm_hotlist_task.done():
        return
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        _prewarm_lock_fd = open(_LOCK_FILE, "w")
        fcntl.flock(_prewarm_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        logger.info("orderbook prewarm: another worker holds the lock — file consumer only")
        _prewarm_lock_fd = None
        return
    logger.info("orderbook prewarm owner started: top=%d, poll=%.1fs, dump=%.1fs",
                PREWARM_TOP_N, POLL_INTERVAL, PREWARM_DUMP_S)
    _prewarm_hotlist_task = asyncio.create_task(_prewarm_hotlist_loop())
    _prewarm_dump_task = asyncio.create_task(_prewarm_dump_loop())


def stop_prewarm() -> None:
    global _prewarm_hotlist_task, _prewarm_dump_task, _prewarm_lock_fd
    for t in (_prewarm_hotlist_task, _prewarm_dump_task):
        if t and not t.done():
            t.cancel()
    _prewarm_hotlist_task = None
    _prewarm_dump_task = None
    from backend.services.orderbook_ws import stop_ws_manager
    stop_ws_manager()
    if _prewarm_lock_fd:
        try: _prewarm_lock_fd.close()
        except Exception: pass
    _prewarm_lock_fd = None
