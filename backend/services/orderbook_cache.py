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
import threading
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
FIRST_WAIT      = 0.7    # max cold-start wait for first data
STALE_FALLBACK  = 10.0   # still serve local-cache data if younger than this
# Two-tier freshness:
#   FILE_FRESH    — green indicator, arb compute prefers these. Prices considered live.
#   FILE_DEGRADED — yellow indicator, still fed to arb compute. Prices may lag but are
#                   within acceptable bounds (outlier filter catches stale-gap cases).
#   STALE_SERVE   — hard ceiling. Beyond this, arb excludes the pair entirely.
# Earlier version had FRESH==STALE_SERVE==5s, which caused pairs to disappear from arb
# every time a single WS heartbeat was late — flickering UI and pairs with half-dead
# orderbooks. Split lets a single delayed tick degrade to yellow instead of vanishing.
FILE_FRESH_MAX  = 5.0
FILE_DEGRADED_MAX = 15.0
STALE_SERVE_MAX = 30.0

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


# ── Per-exchange accepted `limit` values ────────────────────────────────────
# Some venues silently return empty books when the requested limit isn't in
# their canonical set (Aster / Binance-fork behaviour). Round up to the
# smallest acceptable value — serving 20 levels when the caller asked for 12
# is strictly better than serving zero.
_VALID_LIMITS = {
    "binance":  [5, 10, 20, 50, 100, 500, 1000],   # /fapi/v1/depth
    "aster":    [5, 10, 20, 50, 100, 500, 1000],   # Binance fork
    "bybit":    [1, 50, 200, 500, 1000],           # /v5/market/orderbook
    "bitget":   [5, 15, 50, 100, 200, 1000],       # /mix/market/merge-depth
    "mexc":     [5, 10, 20, 50, 100, 200, 500, 1000],  # contract depth
    "okx":      [1, 5, 10, 20, 50, 100, 200, 400],     # /market/books
    "gate":     [5, 10, 20, 50, 100],              # /futures/usdt/order_book
}


def _canonical_limit(exchange: str, limit: int) -> int:
    valid = _VALID_LIMITS.get(exchange)
    if not valid:
        return limit
    for v in valid:
        if v >= limit:
            return v
    return valid[-1]


# ── Direct per-exchange fetch ────────────────────────────────────────────────
async def _fetch_direct(exchange: str, symbol: str, limit: int) -> dict | None:
    c = _arb_http
    limit = _canonical_limit(exchange, limit)
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
            limit_a = _canonical_limit("aster", limit)
            r = await c.get(f"https://fapi.asterdex.com/fapi/v1/depth?symbol={symbol}USDT&limit={limit_a}")
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


def _file_lookup(key: str, max_age: float = FILE_FRESH_MAX) -> dict | None:
    """Return file-cache data for key if younger than max_age, else None."""
    _refresh_file_memo()
    entry = _file_memo.get(key)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > max_age:
        return None
    return entry.get("data")


def _file_lookup_stale(key: str) -> tuple[dict | None, float]:
    """Return (data, age_in_seconds) from file cache regardless of freshness,
    bounded by STALE_SERVE_MAX. Used for fast-path responses that would
    otherwise be empty — better to show 30s-old orderbook than nothing."""
    _refresh_file_memo()
    entry = _file_memo.get(key)
    if not entry:
        return None, float("inf")
    age = time.time() - entry.get("ts", 0)
    if age > STALE_SERVE_MAX:
        return None, age
    return entry.get("data"), age


# ── Public API ───────────────────────────────────────────────────────────────
_PENDING_SUBS_FILE = os.path.join(_CACHE_DIR, "pending_subs.json")
# Long-lived "user hot list" — pairs that at least one active WS client
# subscribed to in the last USER_SUBS_TTL seconds. The prewarm owner uses it
# to keep pair WS subscriptions alive across prune cycles so the user's /arb
# tab doesn't go blank after the top-N list rotates.
_USER_SUBS_FILE = os.path.join(_CACHE_DIR, "user_subs.json")
USER_SUBS_TTL = 1200.0  # 20 min


def _queue_subscribe_request(exchange: str, symbol: str) -> None:
    """Non-owner workers can't call WSManager directly (it runs only on the
    prewarm-owner worker). Drop a request on a shared JSON file; the owner
    drains it every prewarm tick and issues the actual subscribe."""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        pending: dict[str, list[str]] = {}
        if os.path.exists(_PENDING_SUBS_FILE):
            try:
                with open(_PENDING_SUBS_FILE) as f:
                    pending = json.load(f) or {}
            except Exception:
                pending = {}
        syms = set(pending.get(exchange) or [])
        if symbol in syms:
            return
        syms.add(symbol)
        pending[exchange] = sorted(syms)
        tmp = _PENDING_SUBS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(pending, f, separators=(",", ":"))
        os.replace(tmp, _PENDING_SUBS_FILE)
    except Exception as exc:
        logger.debug("queue_subscribe_request failed: %s", exc)


def drain_pending_subs() -> dict[str, list[str]]:
    """Owner-worker helper — returns accumulated requests and clears the file."""
    try:
        if not os.path.exists(_PENDING_SUBS_FILE):
            return {}
        with open(_PENDING_SUBS_FILE) as f:
            pending = json.load(f) or {}
        os.remove(_PENDING_SUBS_FILE)
        return pending
    except Exception:
        return {}


def touch_user_sub(exchange: str, symbol: str) -> None:
    """Stamp a pair as recently requested by an active WS client. Any web
    worker can call this — the prewarm owner reads the file on every tick
    and keeps matching pairs subscribed even when they drop out of the
    top-N arb hot list. File grows bounded (~few dozen KB) because entries
    older than USER_SUBS_TTL are pruned on every read."""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        key = f"{exchange.lower()}:{symbol.upper()}"
        now = time.time()
        data: dict = {}
        if os.path.exists(_USER_SUBS_FILE):
            try:
                with open(_USER_SUBS_FILE) as f:
                    data = json.load(f) or {}
            except Exception:
                data = {}
        data[key] = now
        # Opportunistic prune so the file doesn't grow unbounded under load.
        cutoff = now - USER_SUBS_TTL
        data = {k: ts for k, ts in data.items() if isinstance(ts, (int, float)) and ts >= cutoff}
        tmp = _USER_SUBS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, _USER_SUBS_FILE)
    except Exception as exc:
        logger.debug("touch_user_sub failed: %s", exc)


def read_user_subs() -> dict[str, list[str]]:
    """Owner-worker helper — returns the live user-hot-list grouped by exchange.
    Entries older than USER_SUBS_TTL are filtered out."""
    try:
        if not os.path.exists(_USER_SUBS_FILE):
            return {}
        with open(_USER_SUBS_FILE) as f:
            data = json.load(f) or {}
    except Exception:
        return {}
    cutoff = time.time() - USER_SUBS_TTL
    out: dict[str, list[str]] = {}
    for key, ts in data.items():
        if not isinstance(ts, (int, float)) or ts < cutoff:
            continue
        ex, _, sym = key.partition(":")
        if not ex or not sym:
            continue
        out.setdefault(ex, []).append(sym)
    return out


_REST_FALLBACK_TTL = 12.0           # memory TTL for a REST fallback fetch
_rest_fallback_inflight: dict[str, asyncio.Task] = {}
_rest_fallback_inflight_lock = asyncio.Lock()

# Cap REST fallback depth to what each exchange's WS actually streams.
# If REST returns 50 but WS pushes 20, the next WS tick would shrink the
# visible book and look like "half the orders disappeared". Matching the
# depth keeps the display stable.
_WS_DEPTH = {
    "binance":     20,
    "aster":       20,
    "gate":        20,
    "mexc":        20,
    "hyperliquid": 20,
    "kucoin":      50,
    "bingx":      100,
    "whitebit":   100,
    "bybit":      200,
    "okx":        200,
    "bitget":     200,
}


async def _rest_fallback(exchange: str, symbol: str, limit: int) -> dict | None:
    """Single-shot REST fetch, deduplicated per key so parallel 150ms polls
    don't spawn concurrent REST hits. Caches into _book_cache on success."""
    key = f"{exchange}:{symbol}"
    # Never request more depth from REST than the subsequent WS push will
    # deliver — otherwise the book shrinks visibly when WS catches up.
    req_limit = min(limit, _WS_DEPTH.get(exchange, limit))

    async def _do():
        try:
            data = await _fetch_direct(exchange, symbol, req_limit)
        except Exception:
            data = None
        if data and (data.get("bids") or data.get("asks")):
            now = time.time()
            entry = _book_cache.setdefault(key, {})
            entry["data"] = data
            entry["ts"] = now
            entry["last_request"] = now
            entry["source"] = "rest"
            return data
        return None

    # Hold the lock only long enough to check/attach the inflight task —
    # the await is OUTSIDE the lock so a slow exchange doesn't stall
    # parallel fetches for every other key.
    async with _rest_fallback_inflight_lock:
        existing = _rest_fallback_inflight.get(key)
        if existing and not existing.done():
            task = existing
            owner = False
        else:
            task = asyncio.create_task(_do())
            _rest_fallback_inflight[key] = task
            owner = True
    try:
        return await task
    finally:
        if owner:
            async with _rest_fallback_inflight_lock:
                _rest_fallback_inflight.pop(key, None)


async def get_cached_orderbook(exchange: str, symbol: str, limit: int = 50) -> dict:
    exchange = exchange.lower()
    symbol = symbol.upper()
    key = f"{exchange}:{symbol}"
    now = time.time()

    # 1. Local memory — fastest (WS pushes land here directly).
    # Treat an empty book (no bids AND no asks) as a cache miss — some WS
    # streams briefly deliver an empty snapshot before the first real tick,
    # and we'd otherwise serve the empty book forever until TTL.
    entry = _book_cache.get(key)
    if entry and now - entry.get("ts", 0) < STALE_FALLBACK:
        d = entry.get("data") or {}
        if (d.get("bids") or d.get("asks")):
            entry["last_request"] = now
            return d

    # 2. Shared file cache — fresh path (<5s old). Same empty-book guard.
    fd = _file_lookup(key)
    if fd and (fd.get("bids") or fd.get("asks")):
        return fd

    from backend.services.orderbook_ws import is_ws_supported, get_manager

    # 3. Subscribe via WS (owner worker) or queue for owner (non-owner worker).
    if is_ws_supported(exchange):
        mgr = get_manager()
        if mgr:
            mgr.subscribe(exchange, [symbol])
        else:
            _queue_subscribe_request(exchange, symbol)

    # 4. REST fallback — parallel to WS subscribe. Gives us instant data
    #    (~200-400ms) for any symbol, even non-prewarmed ones, while the
    #    WS subscribe warms up in the background for sub-second updates.
    rest_data = await _rest_fallback(exchange, symbol, limit)
    if rest_data:
        return rest_data

    # 5. REST failed — serve file data ONLY if younger than STALE_SERVE_MAX.
    stale_data, stale_age = _file_lookup_stale(key)
    if stale_data and (stale_data.get("bids") or stale_data.get("asks")):
        if stale_age > FILE_FRESH_MAX:
            logger.info(
                "orderbook %s %s: serving %.1fs-old file data (ceiling %.1fs)",
                exchange, symbol, stale_age, STALE_SERVE_MAX,
            )
        return stale_data

    # 6. Nothing available — brief wait for WS first push, then give up.
    deadline = now + FIRST_WAIT
    while time.time() < deadline:
        await asyncio.sleep(0.05)
        e = _book_cache.get(key) or {}
        d = e.get("data") or {}
        if d.get("bids") or d.get("asks"):
            e["last_request"] = time.time()
            return d
        fd2 = _file_lookup(key)
        if fd2 and (fd2.get("bids") or fd2.get("asks")):
            return fd2

    # 7. Non-WS exchange cold start: kick the polling loop
    if not is_ws_supported(exchange):
        async with _lock:
            entry = _book_cache.setdefault(key, {})
            entry["last_request"] = now
            task = _pollers.get(key)
            if not task or task.done():
                _pollers[key] = asyncio.create_task(_poll_loop(key, exchange, symbol, limit))

    return (_book_cache.get(key) or {}).get("data") or {"bids": [], "asks": []}


def top_levels(exchange: str, symbol: str) -> tuple[float, float] | None:
    """Sync accessor (best_bid, best_ask). Checks local memory then file
    cache. Rejects entries older than FILE_DEGRADED_MAX — prices >15s old
    are unreliable for arb compute. Cap is generous vs the original 5s
    because the outlier filter catches truly stale books (the KuCoin RAVE
    incident was 30+ minutes stale, not 10 seconds)."""
    key = f"{exchange.lower()}:{symbol.upper()}"
    data: dict | None = None
    now = time.time()
    entry = _book_cache.get(key)
    if entry:
        ts = entry.get("ts", 0)
        if now - ts <= FILE_DEGRADED_MAX:
            data = entry.get("data")
    if not data:
        data = _file_lookup(key, max_age=FILE_DEGRADED_MAX)
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


def freshness_by_exchange() -> dict[str, dict]:
    """Orderbook freshness aggregated per exchange. Used by /exchange-health
    to show a second freshness indicator alongside the funding-WS one.
    Reads from in-memory `_book_cache` (fetcher/owner workers) and from the
    shared file cache (other workers), taking the fresher of the two.

    Returns {exchange: {min_age_s, fresh, degraded, stale, total, healthy}}
      · min_age_s: age of freshest book on that exchange (None if no books).
      · fresh:    count with age <= FILE_FRESH_MAX     (5s)  — green
      · degraded: count with age <= FILE_DEGRADED_MAX (15s)  — yellow
      · stale:    count older than FILE_DEGRADED_MAX         — red
      · healthy:  fresh > 0 (at least one live pair)
    """
    _refresh_file_memo()
    now = time.time()
    per_ex: dict[str, dict] = {}

    def _ingest(key: str, ts: float) -> None:
        if ":" not in key:
            return
        ex = key.split(":", 1)[0]
        age = max(0.0, now - ts)
        b = per_ex.setdefault(ex, {"fresh": 0, "degraded": 0, "stale": 0, "min_age_s": float("inf")})
        if age <= FILE_FRESH_MAX:
            b["fresh"] += 1
        elif age <= FILE_DEGRADED_MAX:
            b["degraded"] += 1
        else:
            b["stale"] += 1
        if age < b["min_age_s"]:
            b["min_age_s"] = age

    seen: set[str] = set()
    for key, entry in _book_cache.items():
        ts = entry.get("ts", 0)
        if ts:
            _ingest(key, ts)
            seen.add(key)
    for key, entry in _file_memo.items():
        if key in seen:
            continue
        ts = entry.get("ts", 0)
        if ts:
            _ingest(key, ts)

    for b in per_ex.values():
        b["total"] = b["fresh"] + b["degraded"] + b["stale"]
        b["healthy"] = b["fresh"] > 0
        b["min_age_s"] = None if b["min_age_s"] == float("inf") else round(b["min_age_s"], 2)
    return per_ex


# ── Prewarm owner loops (single worker) ──────────────────────────────────────
PREWARM_TOP_N        = 80
def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return float(v) if v else default
    except (TypeError, ValueError):
        return default

PREWARM_HOTLIST_S    = _env_float("AVALANT_PREWARM_HOTLIST_S", 4.0)
# refresh hot list in lockstep with arb broadcast (3-4s)
PREWARM_DUMP_S       = _env_float("AVALANT_PREWARM_DUMP_S", 0.5)
# snapshot to file
# Prune WS subscriptions down to the current hot-list every N ticks.
# At PREWARM_HOTLIST_S=4s, _PRUNE_EVERY=30 → prune roughly every 2 minutes.
_PRUNE_EVERY         = 30
_prewarm_tick_counter = [0]

_prewarm_hotlist_task: asyncio.Task | None = None
_prewarm_dump_thread:  threading.Thread | None = None
_prewarm_dump_stop:    threading.Event | None = None
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
    from backend.services.arbitrage_service import get_arbitrage_opportunities, get_funding_data
    from backend.services.orderbook_ws import is_ws_supported, start_ws_manager
    from collections import defaultdict
    while True:
        try:
            data = await get_arbitrage_opportunities()
            opps = data.get("opportunities", [])[:PREWARM_TOP_N]
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

            # Arb now REQUIRES orderbook on both sides, so a cold start would
            # leave us with zero opps and never warm anything. Seed the hot
            # list from cross-listed funding data: pick the highest-volume
            # pairs that appear on >=2 exchanges. This bootstraps the book,
            # arb fills in, and then the loop above takes over.
            try:
                fd = await get_funding_data()
                by_sym: dict[str, list[dict]] = defaultdict(list)
                for r in fd.get("rows") or []:
                    by_sym[r["symbol"]].append(r)
                cross = [(sym, rs) for sym, rs in by_sym.items() if len(rs) >= 2]
                cross.sort(
                    key=lambda kv: max((float(r.get("volume_usd") or 0) for r in kv[1]), default=0),
                    reverse=True,
                )
                for sym, rs in cross[:PREWARM_TOP_N]:
                    for r in rs:
                        ex = r.get("exchange")
                        if not ex:
                            continue
                        if is_ws_supported(ex):
                            ws_subs.setdefault(ex, set()).add(sym)
                        else:
                            rest_pairs.add((ex, sym))
            except Exception as exc:
                logger.debug("prewarm seed from funding failed: %s", exc)

            # Pick up any ad-hoc subscribe requests queued by non-owner workers
            pending = drain_pending_subs()
            for ex, syms in pending.items():
                if not is_ws_supported(ex):
                    for s in syms:
                        rest_pairs.add((ex, s))

            # Keep pairs with an active /arb viewer subscribed even if they
            # drop out of the top-N hot list. Without this, /arb goes blank
            # every _PRUNE_EVERY ticks when set_symbols() rotates the
            # subscription set.
            user_hot = read_user_subs()
            for ex, syms in user_hot.items():
                if is_ws_supported(ex):
                    ws_subs.setdefault(ex, set()).update(syms)
                else:
                    for s in syms:
                        rest_pairs.add((ex, s))

            # WS subscriptions: on every tick, just ADD new hot-list symbols
            # (cheap — sends a subscribe frame). Every _PRUNE_EVERY ticks we
            # run set_symbols() instead, which replaces the set and forces
            # a reconnect to drop stale subscriptions. That keeps the live
            # topic count bounded without reconnecting every 4 seconds.
            _prewarm_tick_counter[0] += 1
            prune = (_prewarm_tick_counter[0] % _PRUNE_EVERY) == 0
            mgr = start_ws_manager()
            for ex, syms in ws_subs.items():
                if prune:
                    mgr.set_symbols(ex, list(syms))
                else:
                    mgr.subscribe(ex, list(syms))

            # Ad-hoc subscribes from non-owner workers are ADDITIVE — user
            # just opened /arb for a non-prewarmed symbol.
            for ex, syms in pending.items():
                if is_ws_supported(ex) and syms:
                    mgr.subscribe(ex, list(syms))

            # REST: spawn pollers for exchanges without WS support (perp DEX + slow CEX)
            for ex, sym in rest_pairs:
                await _prewarm_start_poller(ex, sym)

            # REST backstop symbol-push DISABLED — see start_prewarm() comment.
            logger.info(
                "orderbook prewarm: ws=%s rest_pairs=%d (%d opps)",
                {ex: len(s) for ex, s in ws_subs.items()}, len(rest_pairs), len(opps),
            )
        except Exception as exc:
            logger.warning("prewarm hot-list error: %s", exc)
        await asyncio.sleep(PREWARM_HOTLIST_S)


def _prewarm_dump_loop_sync(stop_evt: threading.Event) -> None:
    """Pure-thread books.json dumper.

    Was previously an asyncio task — under heavy fetcher load
    (spot/dex compute + orderbook pollers) the 0.5s sleep stretched to
    15-20s, leaving books.json frozen and the web role serving
    stale/empty orderbooks (and Aster disappearing entirely because its
    poller couldn't keep up).

    Runs in a daemon thread so file IO is fully decoupled from the
    event loop.
    """
    os.makedirs(_CACHE_DIR, exist_ok=True)
    _last_stats_log = 0.0
    while not stop_evt.is_set():
        try:
            # Use STALE_SERVE_MAX (30s) as the cutoff — not FILE_FRESH_MAX (5s).
            # WS adapters drop every 30-60s (1011 keepalive / handshake bursts
            # affect several venues at once). With the old 5s cutoff the file
            # went to {} during those bursts, which made the UI orderbook
            # panel empty and the /ws/book broadcaster had nothing to push.
            # Readers already have `ts` on every entry and apply their own
            # freshness logic, so including up to 30s-old entries is safe.
            cutoff = time.time() - STALE_SERVE_MAX
            snapshot: dict[str, dict] = {}
            total_entries = len(_book_cache)
            stale_kept = 0
            for key, entry in list(_book_cache.items()):
                ts = entry.get("ts", 0)
                if ts < cutoff:
                    continue
                if ts < time.time() - FILE_FRESH_MAX:
                    stale_kept += 1
                data = entry.get("data")
                if not data:
                    continue
                snapshot[key] = {"data": data, "ts": ts}
            tmp = _BOOKS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snapshot, f, separators=(",", ":"))
            os.replace(tmp, _BOOKS_FILE)
            # Stats once every 30s — lets us see if the WS stream is
            # healthy without grepping for individual pair updates.
            now = time.time()
            if now - _last_stats_log >= 30.0:
                _last_stats_log = now
                logger.info(
                    "orderbook dump: %d fresh + %d stale = %d entries in cache (%d in file)",
                    len(snapshot) - stale_kept, stale_kept, total_entries, len(snapshot),
                )
        except Exception as exc:
            logger.warning("prewarm dump error: %s", exc)
        stop_evt.wait(PREWARM_DUMP_S)


def start_prewarm() -> None:
    """Attempt to become the prewarm owner. Only one worker wins the lock;
    others become passive file readers with no added polling load."""
    global _prewarm_hotlist_task, _prewarm_dump_thread, _prewarm_dump_stop, _prewarm_lock_fd
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
    _prewarm_dump_stop = threading.Event()
    _prewarm_dump_thread = threading.Thread(
        target=_prewarm_dump_loop_sync,
        args=(_prewarm_dump_stop,),
        name="orderbook-dump",
        daemon=True,
    )
    _prewarm_dump_thread.start()
    # REST backstop DISABLED — 11 adapters × 6-12 workers caused GIL
    # contention that starved the asyncio event loop for the WS adapters,
    # net-regressed arb availability vs pre-fix (opp_count median 83→13,
    # max empty-streak 28s→252s). Code is kept in backend/services/orderbook_rest/
    # for a future redesign (asyncio-based or process-isolated).
    # from backend.services.orderbook_rest import start_rest_backstops
    # start_rest_backstops()


def stop_prewarm() -> None:
    global _prewarm_hotlist_task, _prewarm_dump_thread, _prewarm_dump_stop, _prewarm_lock_fd
    if _prewarm_hotlist_task and not _prewarm_hotlist_task.done():
        _prewarm_hotlist_task.cancel()
    if _prewarm_dump_stop is not None:
        _prewarm_dump_stop.set()
    _prewarm_hotlist_task = None
    _prewarm_dump_thread = None
    _prewarm_dump_stop = None
    try:
        from backend.services.orderbook_rest import stop_rest_backstops
        stop_rest_backstops()
    except Exception:
        pass
    from backend.services.orderbook_ws import stop_ws_manager
    stop_ws_manager()
    if _prewarm_lock_fd:
        try: _prewarm_lock_fd.close()
        except Exception: pass
    _prewarm_lock_fd = None
