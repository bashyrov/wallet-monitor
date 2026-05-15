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

# Per-exchange dedicated httpx pool. Previously a single shared pool meant
# slow venues (KuCoin/HTX/Aster from Contabo) could occupy connection slots
# and starve fast venues (Binance/Bybit) waiting in pool queue. With dedicated
# pools, each venue's TCP connections live in their own pool — a stuck
# KuCoin doesn't slow down a Binance fetch.
#
# Limits per pool: 30 connections / 15 keepalive — ample for top-100 hot-list
# polling at 0.5s cadence, well under any venue's per-IP connection cap.
# connect=10s for slow venues' TLS handshakes (Contabo edge spends 5-8s on
# KuCoin/HTX). read=4s is enough for orderbook responses.
_HTTP_POOLS: dict[str, httpx.AsyncClient] = {}

def _get_http_for(exchange: str) -> httpx.AsyncClient:
    """Lazy-create a dedicated httpx pool for `exchange`. Threadsafe at import
    time only; subsequent calls just look up the dict."""
    ex = exchange.lower()
    pool = _HTTP_POOLS.get(ex)
    if pool is None:
        pool = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=4.0, write=3.0, pool=1.0),
            headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip, deflate"},
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=30,
                max_keepalive_connections=15,
                keepalive_expiry=30,
            ),
            http2=False,
        )
        _HTTP_POOLS[ex] = pool
    return pool


# Backwards-compat alias used by code paths that aren't venue-aware (e.g.
# the master merger). Keep one large shared pool just for those — no risk
# of competing with the per-venue pools because they go to different hosts.
_arb_http = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10.0, read=4.0, write=3.0, pool=1.0),
    headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip, deflate"},
    follow_redirects=True,
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=40, keepalive_expiry=30),
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

# When running as a per-exchange worker process (see orderbook_ws_worker),
# the dumper narrows its output to a single exchange and writes to
# books.<exchange>.json. The master process then merges those files into
# books.json for web-role readers. Controlled by env var, default off so
# legacy single-process setups are unaffected.
#   AVALANT_OWNED_EXCHANGE=binance python -m backend.services.orderbook_ws_worker
_OWNED_EXCHANGE = os.environ.get("AVALANT_OWNED_EXCHANGE", "").strip().lower() or None
_PER_EX_BOOKS_FILE = (
    os.path.join(_CACHE_DIR, f"books.{_OWNED_EXCHANGE}.json") if _OWNED_EXCHANGE else None
)

_book_cache: dict[str, dict] = {}        # key → {"data": dict, "ts": float, "last_request": float}
_pollers: dict[str, asyncio.Task] = {}
_lock = asyncio.Lock()

# Instrumentation: track time from "key first requested" to "first non-empty
# data in cache" — this is the user-visible latency for In/Out to populate
# when a token enters the hot-list. Logged once per (key, generation) so we
# don't spam on every tick.
_first_req_at: dict[str, float] = {}     # set when prewarm/poller first picks up the key
_first_data_logged: set[str] = set()

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
    "binance":      [5, 10, 20, 50, 100, 500, 1000],   # /fapi/v1/depth
    "aster":        [5, 10, 20, 50, 100, 500, 1000],   # Binance fork
    "bybit":        [1, 50, 200, 500, 1000],           # /v5/market/orderbook
    "bitget":       [5, 15, 50, 100, 200, 1000],       # /mix/market/merge-depth
    "mexc":         [5, 10, 20, 50, 100, 200, 500, 1000],  # contract depth
    "okx":          [1, 5, 10, 20, 50, 100, 200, 400],     # /market/books
    "gate":         [5, 10, 20, 50, 100],              # /futures/usdt/order_book
    "binance_spot": [5, 10, 20, 50, 100, 500, 1000, 5000],
    "bitget_spot":  [1, 5, 15, 50, 100, 200],
    "mexc_spot":    [5, 10, 20, 50, 100, 500, 1000, 5000],
    "okx_spot":     [1, 5, 10, 20, 50, 100, 200, 400],
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
async def _fetch_direct_raw(exchange: str, symbol: str, limit: int) -> dict | None:
    """Inner fetch — returns the dict (possibly with empty bids/asks) or
    raises on infrastructure errors. The empty/error distinction lets
    callers throttle a delisted symbol differently from a rate-limited
    request. Returns None if the exchange isn't recognised."""
    # Per-exchange dedicated pool — slow venues no longer block fast ones.
    c = _get_http_for(exchange)
    limit = _canonical_limit(exchange, limit)
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
    if exchange == "paradex":
        # Paradex returns "asks" / "bids" sorted best-first with string
        # price+size tuples. `depth` caps each side; pass at least 20 so
        # we have enough levels for consensus-style checks elsewhere.
        lim = max(20, min(50, int(limit)))
        r = await c.get(f"https://api.prod.paradex.trade/v1/orderbook/{symbol}-USD-PERP?depth={lim}")
        d = r.json() or {}
        return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
    if exchange == "extended":
        # Extended (StarkNet perpdex) — no WS adapter yet, so this REST
        # path is the only orderbook source. Response shape:
        #   { status, data: { market, bid: [{qty, price}], ask: [...] } }
        r = await c.get(f"https://api.starknet.extended.exchange/api/v1/info/markets/{symbol}-USD/orderbook")
        d = (r.json() or {}).get("data", {}) or {}
        return {"bids": [[float(x["price"]), float(x["qty"])] for x in d.get("bid", [])],
                "asks": [[float(x["price"]), float(x["qty"])] for x in d.get("ask", [])]}
    if exchange == "htx":
        # HTX (Huobi) USDT-margined linear-swap orderbook. step0 = full
        # depth (raw), no aggregation. Response shape:
        #   { status, tick: { bids: [[px, sz], …], asks: […] } }
        r = await c.get(f"https://api.hbdm.com/linear-swap-ex/market/depth?contract_code={symbol}-USDT&type=step0")
        d = (r.json() or {}).get("tick", {}) or {}
        return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
    if exchange == "kraken":
        # Kraken Futures linear perps. Symbol: PF_<TOKEN>USD with the
        # XBT alias for BTC. Orderbook returns bids sorted ASC (worst-first)
        # — flip so callers see best-first.
        token = "XBT" if symbol == "BTC" else symbol
        r = await c.get(
            f"https://futures.kraken.com/derivatives/api/v3/orderbook?symbol=PF_{token}USD"
        )
        d = (r.json() or {}).get("orderBook") or {}
        bids = [[float(x[0]), float(x[1])] for x in d.get("bids", [])]
        asks = [[float(x[0]), float(x[1])] for x in d.get("asks", [])]
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])
        return {"bids": bids, "asks": asks}
    if exchange == "backpack":
        # Backpack perp orderbook. Symbol shape: <BASE>_USDC_PERP.
        # Without &limit they return the full book (~6k levels) — cap to caller's
        # request to keep parse + transfer in line with other venues.
        r = await c.get(
            f"https://api.backpack.exchange/api/v1/depth?symbol={symbol}_USDC_PERP&limit={limit}"
        )
        d = r.json() or {}
        # Backpack sorts asks ASC (best ask first) and bids ASC (worst bid first).
        # Reverse bids so the caller sees best-first like every other venue.
        bids = [[float(x[0]), float(x[1])] for x in d.get("bids", [])]
        bids.sort(key=lambda x: x[0], reverse=True)
        return {"bids": bids,
                "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
    if exchange == "lighter":
        # Lighter zk-perp REST. Uses integer market_id, not symbol — we
        # maintain a 1h-cached symbol→id map fetched from /api/v1/orderBooks.
        # /orderBookOrders returns individual orders both sides; aggregate
        # per-price into levels matching the (price, size) shape.
        mid = await _lighter_market_id(symbol)
        if mid is None:
            return None
        lim = max(1, min(250, int(limit) * 4))  # over-fetch since we aggregate
        r = await c.get(
            f"https://mainnet.zklighter.elliot.ai/api/v1/orderBookOrders?market_id={mid}&limit={lim}"
        )
        d = r.json() or {}
        if d.get("code") != 200:
            return {"bids": [], "asks": []}

        def _aggregate(orders: list, reverse: bool) -> list:
            buckets: dict[float, float] = {}
            for o in orders:
                try:
                    px = float(o.get("price") or 0)
                    sz = float(o.get("remaining_base_amount") or 0)
                except (TypeError, ValueError):
                    continue
                if px <= 0 or sz <= 0:
                    continue
                buckets[px] = buckets.get(px, 0.0) + sz
            return sorted(buckets.items(), key=lambda kv: kv[0], reverse=reverse)

        return {
            "bids": [[p, s] for p, s in _aggregate(d.get("bids") or [], reverse=True)],
            "asks": [[p, s] for p, s in _aggregate(d.get("asks") or [], reverse=False)],
        }
    if exchange == "ethereal":
        # Ethereal SDK only exposes order books via Socket.IO L2Book stream;
        # the REST API has no orderbook endpoint. Round-trip the SDK once
        # per fetch — caller throttles per-pair already (POLL_INTERVAL=500ms),
        # so opening / closing the WS this often is not viable. Return None
        # so the caller falls back to the existing REST poller path's
        # error handling. Live orderbook for Ethereal needs the dedicated
        # SDK-runner adapter (see orderbook_ws/ethereal_sdk.py).
        return None
    # ── Spot venues ──────────────────────────────────────────────────────────
    # Spot orderbooks back the spot leg of /arb?type=spot pages. The WS
    # adapters (BinanceSpotWS, KuCoinSpotWS, …) handle the live path; this
    # block is the REST backstop when WS drops. Without it the spot ladder
    # stays blank with no recovery.
    if exchange == "binance_spot":
        r = await c.get(f"https://api.binance.com/api/v3/depth?symbol={symbol}USDT&limit={limit}")
        d = r.json()
        return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
    if exchange == "bybit_spot":
        lim = max(1, min(200, int(limit)))
        r = await c.get(f"https://api.bybit.com/v5/market/orderbook?category=spot&symbol={symbol}USDT&limit={lim}")
        d = r.json().get("result", {}) or {}
        return {"bids": [[float(x[0]), float(x[1])] for x in d.get("b", [])],
                "asks": [[float(x[0]), float(x[1])] for x in d.get("a", [])]}
    if exchange == "okx_spot":
        r = await c.get(f"https://www.okx.com/api/v5/market/books?instId={symbol}-USDT&sz={limit}")
        d = (r.json().get("data") or [{}])[0] or {}
        return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
    if exchange == "gate_spot":
        lim = max(1, min(100, int(limit)))
        r = await c.get(
            f"https://api.gateio.ws/api/v4/spot/order_book?currency_pair={symbol}_USDT&limit={lim}&with_id=false"
        )
        d = r.json() or {}
        return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
    if exchange == "kucoin_spot":
        # KuCoin spot offers only fixed-depth public endpoints (level2_20 /
        # level2_100). Pick the smallest one that covers the request.
        depth = 20 if limit <= 20 else 100
        r = await c.get(
            f"https://api.kucoin.com/api/v1/market/orderbook/level2_{depth}?symbol={symbol}-USDT"
        )
        d = (r.json() or {}).get("data", {}) or {}
        return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
    if exchange == "bitget_spot":
        r = await c.get(
            f"https://api.bitget.com/api/v2/spot/market/merge-depth?symbol={symbol}USDT&limit={limit}"
        )
        d = (r.json() or {}).get("data", {}) or {}
        return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
    if exchange == "bingx_spot":
        lim = max(5, min(100, int(limit)))
        r = await c.get(
            f"https://open-api.bingx.com/openApi/spot/v1/market/depth?symbol={symbol}-USDT&limit={lim}"
        )
        d = (r.json() or {}).get("data", {}) or {}
        return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
    if exchange == "htx_spot":
        # HTX spot uses lowercase symbol + step0 (no aggregation).
        sym = (symbol + "usdt").lower()
        r = await c.get(f"https://api.huobi.pro/market/depth?symbol={sym}&type=step0&depth=20")
        d = (r.json() or {}).get("tick", {}) or {}
        return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
    if exchange == "mexc_spot":
        r = await c.get(f"https://api.mexc.com/api/v3/depth?symbol={symbol}USDT&limit={limit}")
        d = r.json() or {}
        return {"bids": [[float(x[0]), float(x[1])] for x in d.get("bids", [])],
                "asks": [[float(x[0]), float(x[1])] for x in d.get("asks", [])]}
    return None


# ── Lighter symbol→market_id cache ───────────────────────────────────────────
_lighter_id_cache: tuple[dict[str, int], float] = ({}, 0.0)
_LIGHTER_ID_TTL = 3600.0  # 1 hour — markets only change on listings
_lighter_id_lock = asyncio.Lock()


async def _lighter_market_id(symbol: str) -> int | None:
    """Resolve Lighter's integer market_id from a symbol like "BTC".

    Lighter exposes /api/v1/orderBooks with {symbol, market_id, market_type,
    status} per market; we cache the perp-only subset for 1h.
    """
    sym = (symbol or "").upper()
    cache, ts = _lighter_id_cache
    if cache and (time.monotonic() - ts) < _LIGHTER_ID_TTL:
        return cache.get(sym)
    async with _lighter_id_lock:
        cache, ts = _lighter_id_cache
        if cache and (time.monotonic() - ts) < _LIGHTER_ID_TTL:
            return cache.get(sym)
        try:
            c = _get_http_for("lighter")
            r = await c.get("https://mainnet.zklighter.elliot.ai/api/v1/orderBooks")
            data = r.json() or {}
            books = data.get("order_books") or []
            new_map: dict[str, int] = {}
            for b in books:
                if (b.get("market_type") or "").lower() != "perp":
                    continue
                if (b.get("status") or "").lower() != "active":
                    continue
                s = (b.get("symbol") or "").upper()
                mid = b.get("market_id")
                if s and isinstance(mid, int):
                    new_map[s] = mid
            globals()["_lighter_id_cache"] = (new_map, time.monotonic())
            return new_map.get(sym)
        except Exception as exc:
            logger.warning("lighter market_id refresh failed: %s", exc)
            return cache.get(sym) if cache else None


# Outcome of a single REST poll. "ok" + non-empty book = healthy.
# "empty" = the venue answered cleanly but has no levels for this symbol
# (delisted, halted, ultra-thin) — back off, but not because the venue
# is unreachable. "error" = network / rate-limit / parse — different
# remediation (retry sooner, open circuit, etc).
class FetchOutcome:
    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"


# ── Per-exchange circuit breaker ─────────────────────────────────────────────
# After N consecutive errors within a short window, pause REST polling for
# the venue for COOLDOWN_S. Prevents one slow/down venue from soaking up
# the per-pool connection budget while spamming the same 5xx in a tight
# loop. Auto-resets on the first success after the cooldown.
_CB_FAIL_THRESHOLD = 5      # errors in WINDOW before tripping
_CB_WINDOW_S = 30.0         # window for the consecutive-error count
_CB_COOLDOWN_S = 60.0       # how long to stay open
_cb_state: dict[str, dict] = {}  # exchange → {"fail_count", "first_fail_at", "open_until"}


def _cb_is_open(exchange: str) -> bool:
    s = _cb_state.get(exchange)
    if not s:
        return False
    open_until = s.get("open_until") or 0.0
    if open_until and time.time() < open_until:
        return True
    return False


def _cb_record_error(exchange: str) -> None:
    now = time.time()
    s = _cb_state.setdefault(exchange, {"fail_count": 0, "first_fail_at": 0.0, "open_until": 0.0})
    # Reset window if too much time passed since the first failure
    if s["first_fail_at"] and (now - s["first_fail_at"]) > _CB_WINDOW_S:
        s["fail_count"] = 0
        s["first_fail_at"] = now
    if s["fail_count"] == 0:
        s["first_fail_at"] = now
    s["fail_count"] += 1
    if s["fail_count"] >= _CB_FAIL_THRESHOLD:
        s["open_until"] = now + _CB_COOLDOWN_S
        logger.warning(
            "circuit breaker: %s opened — %d errors in %.1fs, paused %ds",
            exchange, s["fail_count"], now - s["first_fail_at"], int(_CB_COOLDOWN_S),
        )


def _cb_record_success(exchange: str) -> None:
    s = _cb_state.get(exchange)
    if s and (s.get("fail_count") or s.get("open_until")):
        if s.get("open_until"):
            logger.info("circuit breaker: %s closed — first success after cooldown", exchange)
        s["fail_count"] = 0
        s["first_fail_at"] = 0.0
        s["open_until"] = 0.0


async def _fetch_direct_with_status(exchange: str, symbol: str, limit: int) -> tuple[str, dict | None, str | None]:
    """Returns (outcome, data, error). Outcome is one of FetchOutcome.*."""
    if _cb_is_open(exchange):
        return FetchOutcome.ERROR, None, f"{exchange} paused by circuit breaker"
    try:
        data = await _fetch_direct_raw(exchange, symbol, limit)
    except Exception as exc:
        logger.debug("orderbook fetch %s/%s failed: %s", exchange, symbol, exc)
        _cb_record_error(exchange)
    return FetchOutcome.OK, data, None


async def _fetch_direct(exchange: str, symbol: str, limit: int) -> dict | None:
    """Backwards-compatible wrapper used by REST fallback paths that only
    care about the data, treating empty and error as the same shape."""
    outcome, data, _err = await _fetch_direct_with_status(exchange, symbol, limit)
    if outcome == FetchOutcome.OK:
        return data
    return None


# ── Poller task ──────────────────────────────────────────────────────────────
# Consecutive-failure thresholds and back-off windows. Empty books (delisted /
# halted / ultra-thin) get a much longer back-off than errors (network /
# rate-limit) — empty pairs aren't going to recover in the next 3 seconds, so
# keep hammering is pure waste of REST budget. Errors usually clear within
# seconds (rate-limit windows expire, connections recover) so we throttle
# more gently.
_EMPTY_BACKOFF_SHORT_S = 30.0   # after 5 consecutive empties → poll once / 30s
_EMPTY_BACKOFF_LONG_S = 300.0   # after 20 consecutive empties → poll once / 5m
_ERROR_BACKOFF_S = 5.0          # after 5 consecutive errors  → 5s extra sleep


async def _poll_loop(key: str, exchange: str, symbol: str, limit: int) -> None:
    consecutive_empty = 0
    consecutive_error = 0
    try:
        while True:
            entry = _book_cache.get(key)
            if not entry or time.time() - entry.get("last_request", 0) > IDLE_TIMEOUT:
                logger.info("orderbook poller idle, stopping: %s", key)
                return

            # Skip the REST fetch if WS has already pushed a fresh book.
            # Lets us run the poller as a "WS dropout" backstop without
            # generating REST traffic when WS is healthy.
            ts = entry.get("ts", 0)
            if ts and time.time() - ts < FILE_FRESH_MAX:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            outcome, data, err = await _fetch_direct_with_status(exchange, symbol, limit)
            # Permanent give-up on "unsupported exchange" — we have no REST
            # handler, no amount of retrying will fix it. Logged once so
            # the source of the bad subscription can be tracked, then the
            # poller exits and stops generating noise.
            if outcome == FetchOutcome.ERROR and err and err.startswith("unsupported exchange"):
                logger.warning(
                    "orderbook poller giving up: %s — %s (no REST handler; "
                    "this poller should not have been started)",
                    key, err,
                )
                return
            if outcome == FetchOutcome.OK:
                entry["data"] = data
                entry["ts"] = time.time()
                consecutive_empty = 0
                consecutive_error = 0
                if key not in _first_data_logged:
                    t0 = _first_req_at.get(key)
                    if t0 is not None:
                        logger.info(
                            "orderbook first-data %s via REST in %.2fs",
                            key, time.time() - t0,
                        )
                        _first_data_logged.add(key)
            elif outcome == FetchOutcome.EMPTY:
                consecutive_empty += 1
                consecutive_error = 0
                if consecutive_empty == 1 or consecutive_empty in (5, 20) or consecutive_empty % 100 == 0:
                    logger.info("orderbook poll empty: %s (streak=%d) — venue returned no levels", key, consecutive_empty)
                if consecutive_empty >= 20:
                    await asyncio.sleep(_EMPTY_BACKOFF_LONG_S)
                    continue
                if consecutive_empty >= 5:
                    await asyncio.sleep(_EMPTY_BACKOFF_SHORT_S)
                    continue
            else:  # FetchOutcome.ERROR
                consecutive_error += 1
                consecutive_empty = 0
                if consecutive_error == 1 or consecutive_error % 10 == 0:
                    logger.warning(
                        "orderbook poll error: %s (streak=%d) — %s",
                        key, consecutive_error, err or "?",
                    )
                if consecutive_error >= 5:
                    await asyncio.sleep(_ERROR_BACKOFF_S)
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


def _load_books_snapshot() -> dict | None:
    """One-shot read of `books.json` that returns the entire decoded dict.

    Used by the arb compute loop so it can iterate thousands of symbol-
    exchange combos without re-parsing the file each time. Reading once is
    ~600 ms on a 4.5 MB file, vs `_refresh_file_memo` re-reading up to
    15× per compute under the 200 ms merger mtime churn (profile showed
    that was 81 % of compute time).
    """
    try:
        st = os.stat(_BOOKS_FILE)
        # If the file is older than our hard stale ceiling, pretend it isn't
        # there — caller falls back to `top_levels`, which applies its own
        # staleness rules.
        if time.time() - st.st_mtime > STALE_SERVE_MAX:
            return None
        with open(_BOOKS_FILE, "rb") as f:
            return json.loads(f.read())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


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
    older than USER_SUBS_TTL are pruned on every read.

    Phase-6 rollout: also publish to the Redis `book:subscribe` channel
    so the Go fetcher (running alongside in shadow / cutover) picks up
    the on-demand subscribe in <100ms instead of waiting for the
    prewarm cycle."""
    key = f"{exchange.lower()}:{symbol.upper()}"
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
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
    # Fire-and-forget publish to Go fetcher. Never blocks the caller —
    # if Redis is down, the file path above is still authoritative for
    # the Python prewarm owner. Reuses the cached redis client from
    # orderbook_redis (single connection per worker, lazy + backoff).
    try:
        from backend.services.orderbook_redis import _get_client  # noqa
        cli = _get_client()
        if cli is not None:
            cli.publish("book:subscribe", key)
    except Exception:
        pass


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
    "binance_spot": 20,
    "bybit_spot":   50,
    "okx_spot":     50,
    "gate_spot":    20,
    "kucoin_spot": 100,
    "bitget_spot": 100,
    "bingx_spot":  100,
    "htx_spot":     20,
    "mexc_spot":    20,
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

    # Detail page asks for deep books (limit > 30). Prewarm WS subscribes to
    # shallow depth (5-20 levels) for tight In/Out cadence on the screener
    # list, so the WS cache *can't* satisfy a 100/200-level request — go
    # straight to REST for those, which returns full-depth snapshots.
    DEEP_LIMIT_THRESHOLD = 30
    if limit > DEEP_LIMIT_THRESHOLD:
        # Check memory cache first — WS or a recent REST fetch may have
        # populated it. Serving <10s-old in-memory data costs µs vs 200-400ms
        # for a REST round-trip, and the WS updates every ~100-500ms anyway.
        entry = _book_cache.get(key)
        if entry and now - entry.get("ts", 0) < STALE_FALLBACK:
            d = entry.get("data") or {}
            if d.get("bids") or d.get("asks"):
                entry["last_request"] = now
                return d
        # Memory miss → REST fallback.
        rest_data = await _rest_fallback(exchange, symbol, limit)
        if rest_data and (rest_data.get("bids") or rest_data.get("asks")):
            # Trip last_request so the WS poller (if any) keeps the cache
            # warm in case a follow-up shallow request comes in.
            entry = _book_cache.setdefault(key, {})
            entry["last_request"] = now
            return rest_data
        # If REST failed, fall through to the cache/file/WS fallbacks below.

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

    # 1.5 Redis — master merger mirrors every merged entry here with TTL 10 s.
    # O(1) GET replaces the 6.5 MB books.json re-parse that dominated this
    # endpoint's latency for web workers (237-580 ms → 1-3 ms).
    try:
        from backend.services.orderbook_redis import read_book
        rb = read_book(exchange, symbol)
        if rb and now - rb.get("ts", 0) < FILE_FRESH_MAX:
            d = rb.get("data") or {}
            if d.get("bids") or d.get("asks"):
                return d
    except Exception:
        pass

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
        b = per_ex.setdefault(
            ex,
            {"fresh": 0, "degraded": 0, "stale": 0, "min_age_s": float("inf"),
             "_ages": []},
        )
        if age <= FILE_FRESH_MAX:
            b["fresh"] += 1
        elif age <= FILE_DEGRADED_MAX:
            b["degraded"] += 1
        else:
            b["stale"] += 1
        if age < b["min_age_s"]:
            b["min_age_s"] = age
        b["_ages"].append(age)

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
        ages = b.pop("_ages", [])
        if ages:
            ages.sort()
            n = len(ages)
            b["avg_age_s"] = round(sum(ages) / n, 2)
            b["median_age_s"] = round(ages[n // 2], 2)
            b["max_age_s"] = round(ages[-1], 2)
            b["p90_age_s"] = round(ages[min(n - 1, int(n * 0.9))], 2)
        else:
            b["avg_age_s"] = b["median_age_s"] = b["max_age_s"] = b["p90_age_s"] = None
    return per_ex


# ── Prewarm owner loops (single worker) ──────────────────────────────────────
# Top-N opps that get orderbook subscriptions. Drastically reduced from 100
# to 30 — In/Out columns dropped from screener (basis-only display), so the
# orderbook is now only needed for /arb detail page on-demand. Keeping a
# Small hot-list around means clicking a top opp opens the detail page
# with the book already warm. Pairs outside fetch on /arb open
# (1-2s warmup is acceptable for a deliberate click-through). Trimmed
# 30 → 20 — the top-20 by basis cover ~95 % of clicks, and each
# subscription costs CPU on the worker (parse + book-merge + Redis).
# At ~11 venues × 20 = 220 active WS subs (vs 330 prior). Override via
# env if a deployment wants more breadth.
PREWARM_TOP_N        = int(os.environ.get("AVALANT_PREWARM_TOP_N") or 20)
def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return float(v) if v else default
    except (TypeError, ValueError):
        return default

# How often we re-pick the hot-list from current arb opportunities. With
# the smaller top-30 hot-list and basis-only display, the prewarm has much
# less work — but still 2s tick to keep things lively when the top rotates.
PREWARM_HOTLIST_S    = _env_float("AVALANT_PREWARM_HOTLIST_S", 2.0)
# How often we atomically-dump the merged `books.json` for web readers.
# Live-mode: 100 ms. Each worker writes ~300 KB/dump × 11 workers = ~33 MB/s
# to tmpfs — fine on NVMe. Combined with 100 ms master merge + 100 ms WS
# broadcast, end-to-end orderbook latency is ~300 ms (was 500-700 ms).
PREWARM_DUMP_S       = _env_float("AVALANT_PREWARM_DUMP_S", 0.1)
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

    # Exchanges owned by a child worker — we must NOT subscribe to them
    # from the master's in-process WSManager, otherwise we duplicate the
    # WS stream (wasted bandwidth + the master's _book_cache fills but its
    # dump is disabled, so the merger sees stale-looking output).
    # Inverse: when running AS a worker (AVALANT_OWNED_EXCHANGE is set),
    # only subscribe to that exchange — ignore the rest.
    raw_workers = (os.environ.get("AVALANT_WORKER_EXCHANGES") or "").strip()
    worker_owned_exchanges: set[str] = {
        e.strip().lower() for e in raw_workers.split(",") if e.strip()
    } if _OWNED_EXCHANGE is None else set()
    only_exchange = _OWNED_EXCHANGE
    while True:
        try:
            data = await get_arbitrage_opportunities()
            # User-set policy: prewarm follows top-N by ABSOLUTE basis (price
            # spread). The bigger the divergence, the more interesting the
            # pair — orderbook subscription kicks in immediately so In/Out
            # show up the moment the row enters the hotlist.
            _all_opps = list(data.get("opportunities", []) or [])
            _all_opps.sort(key=lambda o: abs(float(o.get("price_spread") or o.get("basis_pct") or 0)), reverse=True)
            opps = _all_opps[:PREWARM_TOP_N]
            ws_subs: dict[str, set[str]] = {}
            rest_pairs: set[tuple[str, str]] = set()
            for o in opps:
                sym = o["symbol"]
                for ex in (o.get("long_exchange"), o.get("short_exchange")):
                    if not ex:
                        continue
                    ex_lc = ex.lower()
                    # Worker scope: only our assigned exchange.
                    if only_exchange and ex_lc != only_exchange:
                        continue
                    # Master scope: skip exchanges a worker owns.
                    if ex_lc in worker_owned_exchanges:
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
                        ex_lc = ex.lower()
                        if only_exchange and ex_lc != only_exchange:
                            continue
                        if ex_lc in worker_owned_exchanges:
                            continue
                        if is_ws_supported(ex):
                            ws_subs.setdefault(ex, set()).add(sym)
                        else:
                            rest_pairs.add((ex, sym))
            except Exception as exc:
                logger.debug("prewarm seed from funding failed: %s", exc)

            # Spot-short opps — subscribe to the spot venue's spot-orderbook
            # WS where we have a binance_spot / bybit_spot / okx_spot adapter.
            # Gives the Spot/Short tab live In/Out columns (the 3 venues we
            # cover hold ~70% of spot-short volume). Ignores other venues
            # silently — basis_pct falls back to ticker-based math.
            try:
                from backend.services.spot_arbitrage_service import (
                    get_spot_arbitrage_opportunities as _sp_get,
                )
                sp_data = await _sp_get()
                _sp_all = list(sp_data.get("opportunities") or [])
                _sp_all.sort(key=lambda o: abs(float(o.get("basis_pct") or 0)), reverse=True)
                for o in _sp_all[:PREWARM_TOP_N]:
                    sym_s = o.get("symbol")
                    spot_ex = (o.get("spot_exchange") or "").lower()
                    short_ex = (o.get("short_exchange") or "").lower()
                    if not sym_s:
                        continue
                    # Subscribe the spot side (if we have a spot adapter)
                    spot_key = f"{spot_ex}_spot"
                    if is_ws_supported(spot_key) and (not only_exchange or spot_key == only_exchange) and spot_key not in worker_owned_exchanges:
                        ws_subs.setdefault(spot_key, set()).add(sym_s)
                    # Short side is always a perp WS — already handled above,
                    # but spot opps may have smaller perp venues not in the
                    # arb top-200; add them explicitly.
                    if is_ws_supported(short_ex) and (not only_exchange or short_ex == only_exchange) and short_ex not in worker_owned_exchanges:
                        ws_subs.setdefault(short_ex, set()).add(sym_s)
            except Exception as exc:
                logger.debug("prewarm spot-opps failed: %s", exc)

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

            # REST backstop for WS-supported venues whose stream is stale.
            # KuCoin/HTX/Aster handshakes intermittently fail from Contabo;
            # without this, hot-list pairs on those venues sit at book_ok=
            # False until the WS finally comes back. Kicking _poll_loop
            # gives them a 1s REST tick so the In/Out cells fill in even
            # when WS is dead. Once WS recovers, _book_cache.ts overrides.
            #
            # Bump last_request directly here so the poll loop doesn't
            # idle-exit after 30s. The loop self-throttles via _book_cache
            # freshness — when WS pushes a fresh book, the poller skips
            # its fetch on the next tick.
            _now = time.time()
            for ex, syms in ws_subs.items():
                for sym in syms:
                    key = f"{ex}:{sym}"
                    e = _book_cache.setdefault(key, {})
                    age = _now - (e.get("ts", 0) or 0)
                    e["last_request"] = _now
                    # Mark when the key first entered prewarm — used to log
                    # the eventual subscribe→first-data latency (REST or WS,
                    # whichever wins). Only set once per cold-start to keep
                    # the metric meaningful.
                    if key not in _first_req_at and key not in _first_data_logged:
                        _first_req_at[key] = _now
                    if age > 10.0:
                        async with _lock:
                            task = _pollers.get(key)
                            if not task or task.done():
                                _pollers[key] = asyncio.create_task(
                                    _poll_loop(key, ex, sym, 50)
                                )

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
            # When run as a per-exchange worker, only dump entries for our
            # assigned exchange — the master merges our file with every
            # other worker's to produce books.json.
            prefix = (_OWNED_EXCHANGE + ":") if _OWNED_EXCHANGE else None
            for key, entry in list(_book_cache.items()):
                if prefix and not key.startswith(prefix):
                    continue
                ts = entry.get("ts", 0)
                if ts < cutoff:
                    continue
                if ts < time.time() - FILE_FRESH_MAX:
                    stale_kept += 1
                data = entry.get("data")
                if not data:
                    continue
                snapshot[key] = {"data": data, "ts": ts}
            out_path = _PER_EX_BOOKS_FILE or _BOOKS_FILE
            tmp = out_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snapshot, f, separators=(",", ":"))
            os.replace(tmp, out_path)
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


def start_prewarm(*, dump_books: bool = True, dump_to_master_file: bool = False) -> None:
    """Attempt to become the prewarm owner. Only one worker wins the lock;
    others become passive file readers with no added polling load.

    `dump_books=False` disables the books.json writer — used by the
    multiprocess master, where the orderbook-ws-master merger produces the
    canonical file by combining per-worker books.<ex>.json slices. Without
    this we'd have two threads racing to write /tmp/avalant_cache/books.json.

    `dump_to_master_file=True` (multiprocess master only) enables a dump
    thread that writes `books.master.json` — the merger picks this file up
    the same way it picks up worker files, so any orderbooks that live only
    in master's `_book_cache` (spot WS adapters, Paradex, etc.) reach the
    shared books.json without racing the merger.
    """
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
    dump_mode = (
        f"{PREWARM_DUMP_S}s (books.master.json)" if dump_to_master_file
        else f"{PREWARM_DUMP_S}s" if dump_books
        else "off (merger owns books.json)"
    )
    logger.info("orderbook prewarm owner started: top=%d, poll=%.1fs, dump=%s",
                PREWARM_TOP_N, POLL_INTERVAL, dump_mode)
    _prewarm_hotlist_task = asyncio.create_task(_prewarm_hotlist_loop())
    if dump_books or dump_to_master_file:
        _prewarm_dump_stop = threading.Event()
        # Override output path when running alongside the merger — keeps us
        # out of its write lane on books.json while still surfacing master-
        # only entries (spot WS, paradex) to consumers via books.master.json.
        if dump_to_master_file:
            global _PER_EX_BOOKS_FILE
            _PER_EX_BOOKS_FILE = os.path.join(_CACHE_DIR, "books.master.json")
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
