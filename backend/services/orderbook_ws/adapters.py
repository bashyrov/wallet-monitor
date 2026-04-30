"""Per-exchange WS adapter implementations."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

import httpx

from .base import WSAdapter

logger = logging.getLogger("avalant.ws")


def _to_book(raw_bids, raw_asks) -> tuple[list, list]:
    """Normalise [price, qty] lists to float pairs."""
    bids = [[float(x[0]), float(x[1])] for x in raw_bids] if raw_bids else []
    asks = [[float(x[0]), float(x[1])] for x in raw_asks] if raw_asks else []
    return bids, asks


# ── Binance Futures (partial-book snapshot) ──────────────────────────────────
# The diff-stream + REST snapshot approach works in theory but Binance rate-
# limits /fapi/v1/depth very aggressively when many symbols are subscribed
# in parallel (returns 418 "I'm a teapot"). Partial-book @depth20 is stable
# under any load and matches the level count shown on the native Binance
# Futures UI (they cap the live book rendering around 20 rows too).
class BinanceWS(WSAdapter):
    name = "binance"
    url = "wss://fstream.binance.com/ws"
    _rest_depth_url = "https://fapi.binance.com/fapi/v1/depth"
    _snap_limit = 1000
    # Binance fstream sends a ping every ~3 min and the websockets lib
    # auto-replies pong. Our own ping_interval=20s flood was triggering
    # 1011 closes after a few minutes (binance edge dropped us as noisy).
    # Let the venue drive keepalive via its own pings.
    ping_interval = None  # type: ignore[assignment]
    ping_timeout = None   # type: ignore[assignment]

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._books: dict[str, dict] = {}

    def on_reconnect(self) -> None:
        self._books.clear()

    def build_subscribe(self, symbols):
        params = [f"{s.lower()}usdt@depth20@100ms" for s in symbols]
        return {"method": "SUBSCRIBE", "params": params, "id": 1}

    def parse_message(self, msg):
        if msg.get("result") is None and "params" in msg:
            return None
        if not isinstance(msg, dict) or "s" not in msg:
            return None
        sym = msg["s"]
        if not sym.endswith("USDT"):
            return None
        token = sym[:-4]
        bids, asks = _to_book(msg.get("b"), msg.get("a"))
        return token, bids, asks


# ── Bybit Linear Perp ─────────────────────────────────────────────────────────
class BybitWS(WSAdapter):
    name = "bybit"
    url = "wss://stream.bybit.com/v5/public/linear"
    subscribe_delay = 0.1  # Bybit accepts up to 10 topics/frame but pauses help on resubscribe bursts
    # Bybit V5 closes the WS with 1011 if the WS-frame ping the websockets
    # library sends gets no reply (Bybit ignores those). They use an
    # app-level ping/pong: client sends {"op":"ping"} every <20s, server
    # replies with {"op":"pong"}. Disable the lib-level pings entirely
    # (ping_interval=None) so the websockets keepalive timer never fires;
    # heartbeat_frame() drives our own.
    ping_interval = None  # type: ignore[assignment]
    ping_timeout = None   # type: ignore[assignment]

    def heartbeat_frame(self):
        return '{"op":"ping"}'

    def __init__(self, update_cb):
        super().__init__(update_cb)
        # Bybit sends snapshot then deltas — maintain local book per symbol
        self._books: dict[str, dict[str, dict]] = {}  # sym → {"bids": {p:q}, "asks": {p:q}}

    def build_subscribe(self, symbols):
        # orderbook.50 = snapshot stream at 20 ms cadence (vs 100 ms delta on
        # .200). 50 levels is plenty for /arb display (shows 14) and arb
        # compute (top-of-book). Trades 5× message rate for 5× freshness.
        args = [f"orderbook.50.{s}USDT" for s in symbols]
        # Bybit limits 10 topics per subscribe frame
        frames = []
        for i in range(0, len(args), 10):
            frames.append({"op": "subscribe", "args": args[i:i + 10]})
        return frames

    def on_reconnect(self) -> None:
        self._books.clear()

    def parse_message(self, msg):
        if msg.get("op") or msg.get("success") is not None:
            return None
        topic = msg.get("topic", "")
        if not topic.startswith("orderbook.50."):
            return None
        sym_pair = topic.split(".")[-1]  # BTCUSDT
        if not sym_pair.endswith("USDT"):
            return None
        token = sym_pair[:-4]
        data = msg.get("data", {})
        msg_type = msg.get("type", "")

        # .50 sends one snapshot then deltas at 20 ms; the delta patch logic
        # is identical to .200 so we reuse it.
        book = self._books.setdefault(token, {"bids": {}, "asks": {}})
        if msg_type == "snapshot":
            book["bids"] = {float(p): float(q) for p, q in data.get("b", [])}
            book["asks"] = {float(p): float(q) for p, q in data.get("a", [])}
        else:  # delta
            for side, key in (("b", "bids"), ("a", "asks")):
                for p, q in data.get(side, []):
                    fp, fq = float(p), float(q)
                    if fq == 0:
                        book[key].pop(fp, None)
                    else:
                        book[key][fp] = fq

        bids = sorted(((p, q) for p, q in book["bids"].items()), key=lambda x: -x[0])[:50]
        asks = sorted(((p, q) for p, q in book["asks"].items()), key=lambda x: x[0])[:50]
        return token, [list(x) for x in bids], [list(x) for x in asks]


# ── OKX Perp (books — 400 levels, snapshot + deltas) ─────────────────────────
class OKXWS(WSAdapter):
    """OKX `books` channel: first message is a full snapshot (action='snapshot'),
    subsequent messages are deltas (action='update') where [price, size, ...]
    entries with size=0 remove that level. We keep a running {price → size}
    dict per symbol and emit the top-20 levels."""

    name = "okx"
    url = "wss://ws.okx.com:8443/ws/v5/public"
    # OKX V5 closes WS with 1011 after 30s of no traffic. Lib-level WS-frame
    # pings get ignored — they want a literal "ping" text frame, server
    # responds "pong". Disable lib pings + drive our own at ~25s.
    ping_interval = None  # type: ignore[assignment]
    ping_timeout = None   # type: ignore[assignment]

    def heartbeat_frame(self):
        return "ping"

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._books: dict[str, dict[str, dict[float, float]]] = {}

    def on_reconnect(self) -> None:
        self._books.clear()

    def build_subscribe(self, symbols):
        args = [{"channel": "books", "instId": f"{s}-USDT-SWAP"} for s in symbols]
        return {"op": "subscribe", "args": args}

    def parse_message(self, msg):
        if msg.get("event"):
            return None
        arg = msg.get("arg", {})
        if arg.get("channel") != "books":
            return None
        inst = arg.get("instId", "")
        if not inst.endswith("-USDT-SWAP"):
            return None
        token = inst.split("-")[0]
        data_list = msg.get("data") or []
        if not data_list:
            return None
        data = data_list[0]
        action = msg.get("action") or data.get("action") or "snapshot"

        book = self._books.setdefault(token, {"bids": {}, "asks": {}})
        if action == "snapshot":
            book["bids"].clear()
            book["asks"].clear()

        for side_key in ("bids", "asks"):
            for lvl in data.get(side_key) or []:
                try:
                    price = float(lvl[0])
                    size = float(lvl[1])
                except (ValueError, IndexError, TypeError):
                    continue
                if size <= 0:
                    book[side_key].pop(price, None)
                else:
                    book[side_key][price] = size

        bids = sorted(book["bids"].items(), key=lambda x: -x[0])[:200]
        asks = sorted(book["asks"].items(), key=lambda x: x[0])[:200]
        return token, [[p, s] for p, s in bids], [[p, s] for p, s in asks]


# ── Bitget Perp (books — 150-level snapshot + deltas) ────────────────────────
class BitgetWS(WSAdapter):
    """Bitget V2 `books` channel: first message action='snapshot' carries the
    full 150-level book, subsequent 'update' messages carry only the changed
    levels. Apply deltas (size=0 removes the level) to preserve full depth."""

    name = "bitget"
    url = "wss://ws.bitget.com/v2/ws/public"
    # Bitget V2 closes the WS with 1011 after ~30s without an app-level
    # "ping" string. The websockets-lib WS-frame ping is ignored (Bitget
    # disconnects). Disable lib pings + send our own app-level "ping"
    # every ~25s via the heartbeat hook.
    ping_interval = None  # type: ignore[assignment]
    ping_timeout = None   # type: ignore[assignment]

    def heartbeat_frame(self):
        return "ping"

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._books: dict[str, dict[str, dict[float, float]]] = {}

    def on_reconnect(self) -> None:
        self._books.clear()

    def build_subscribe(self, symbols):
        args = [{"instType": "USDT-FUTURES", "channel": "books", "instId": f"{s}USDT"} for s in symbols]
        return {"op": "subscribe", "args": args}

    def parse_message(self, msg):
        if msg.get("event"):
            return None
        arg = msg.get("arg", {})
        if arg.get("channel") != "books":
            return None
        inst = arg.get("instId", "")
        if not inst.endswith("USDT"):
            return None
        token = inst[:-4]
        action = msg.get("action") or "snapshot"
        data = (msg.get("data") or [{}])[0]

        book = self._books.setdefault(token, {"bids": {}, "asks": {}})
        if action == "snapshot":
            book["bids"].clear()
            book["asks"].clear()

        for side_key in ("bids", "asks"):
            for lvl in data.get(side_key) or []:
                try:
                    price = float(lvl[0])
                    size = float(lvl[1])
                except (ValueError, IndexError, TypeError):
                    continue
                if size <= 0:
                    book[side_key].pop(price, None)
                else:
                    book[side_key][price] = size

        bids = sorted(book["bids"].items(), key=lambda x: -x[0])[:200]
        asks = sorted(book["asks"].items(), key=lambda x: x[0])[:200]
        return token, [[p, s] for p, s in bids], [[p, s] for p, s in asks]


# ── BingX Perp (depth20 on new URL) ───────────────────────────────────────────
class BingXWS(WSAdapter):
    name = "bingx"
    url = "wss://open-api-swap.bingx.com/swap-market"
    decompress_gzip = True
    # BingX server sends a "Ping" text frame every 5s. Client MUST respond
    # with literal "Pong". Lib-level WS-frame pings get ignored → 1011
    # within seconds. Disable lib pings, respond via pong_for.
    ping_interval = None  # type: ignore[assignment]
    ping_timeout = None   # type: ignore[assignment]

    def pong_for(self, msg):
        # BingX server sends gzipped "Ping" — after decompress it's a string.
        # parse_message receives JSON dicts; pong_for receives the parsed
        # frame BEFORE parse_message. We need the raw frame check.
        if msg == "Ping" or (isinstance(msg, dict) and msg.get("ping")):
            return "Pong"
        return None

    def build_subscribe(self, symbols):
        # @depth20 — 20 levels at 100ms cadence. The earlier @depth5
        # was only top-5 which is fine for In/Out but limits the
        # /arb book pane. depth100 ships at 5-10s which is too slow
        # for our use; depth20 is the sweet spot — 4× more levels at
        # the same 100ms tick rate.
        return [
            {"id": str(i), "reqType": "sub", "dataType": f"{s}-USDT@depth20"}
            for i, s in enumerate(symbols)
        ]

    def parse_message(self, msg):
        dt = msg.get("dataType", "")
        if "@depth" not in dt:
            return None
        pair = dt.split("@")[0]  # "BTC-USDT"
        if not pair.endswith("-USDT"):
            return None
        token = pair.split("-")[0]
        data = msg.get("data", {})
        bids, asks = _to_book(data.get("bids"), data.get("asks"))
        return token, bids, asks


# ── Aster (Binance-compatible) ────────────────────────────────────────────────
class AsterWS(BinanceWS):
    name = "aster"
    url = "wss://fstream.asterdex.com/ws"
    _rest_depth_url = "https://fapi.asterdex.com/fapi/v1/depth"
    # Aster throws 3002 ("request frequency exceeds limit") on rapid subscribe
    # bursts. Space frames out so the full set of ~60 symbols (12 frames of 5)
    # takes ~3.6s instead of firing instantly.
    subscribe_delay = 0.3

    def build_subscribe(self, symbols):
        # Aster rejects large single-frame subscribes under load — chunk by 5
        frames = []
        for i in range(0, len(symbols), 5):
            params = [f"{s.lower()}usdt@depth20@100ms" for s in symbols[i:i + 5]]
            frames.append({"method": "SUBSCRIBE", "params": params, "id": i + 1})
        return frames


# ── Gate.io Futures USDT ──────────────────────────────────────────────────────
class GateWS(WSAdapter):
    name = "gate"
    url = "wss://fx-ws.gateio.ws/v4/ws/usdt"

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._books: dict[str, dict[str, dict[float, float]]] = {}

    def on_reconnect(self) -> None:
        self._books.clear()

    def build_subscribe(self, symbols):
        # `futures.order_book_update` ships a full snapshot on subscribe
        # (event=all) followed by deltas (event=update) every 100ms — way
        # tighter than `futures.order_book` which only re-snapshots on
        # change at unspecified cadence (was 8-12s in practice).
        import time as _t
        frames = []
        for s in symbols:
            frames.append({
                "time": int(_t.time()),
                "channel": "futures.order_book_update",
                "event": "subscribe",
                "payload": [f"{s}_USDT", "100ms", "20"],
            })
        return frames

    def parse_message(self, msg):
        if msg.get("channel") != "futures.order_book_update":
            return None
        ev = msg.get("event")
        if ev not in ("all", "update"):
            return None
        result = msg.get("result") or {}
        contract = result.get("s") or result.get("contract") or ""
        if not contract.endswith("_USDT"):
            return None
        token = contract[:-5]
        book = self._books.setdefault(token, {"bids": {}, "asks": {}})
        if ev == "all":
            book["bids"].clear()
            book["asks"].clear()
        for side_key, raw in (("bids", result.get("b") or result.get("bids") or []),
                              ("asks", result.get("a") or result.get("asks") or [])):
            store = book[side_key]
            for lvl in raw:
                if isinstance(lvl, dict):
                    p_v = lvl.get("p"); q_v = lvl.get("s")
                else:
                    p_v, q_v = (lvl[0], lvl[1]) if isinstance(lvl, (list, tuple)) and len(lvl) >= 2 else (None, None)
                if p_v is None or q_v is None:
                    continue
                try:
                    p = float(p_v); q = float(q_v)
                except (TypeError, ValueError):
                    continue
                if q <= 0:
                    store.pop(p, None)
                else:
                    store[p] = q
        bids = sorted(book["bids"].items(), key=lambda x: -x[0])[:200]
        asks = sorted(book["asks"].items(), key=lambda x: x[0])[:200]
        return token, [[p, s] for p, s in bids], [[p, s] for p, s in asks]


# ── MEXC Futures ──────────────────────────────────────────────────────────────
class MEXCWS(WSAdapter):
    name = "mexc"
    url = "wss://contract.mexc.com/edge"
    # MEXC futures wants {"method":"ping"} every 15s, ignores WS-frame pings.
    ping_interval = None  # type: ignore[assignment]
    ping_timeout = None   # type: ignore[assignment]

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._books: dict[str, dict[str, dict[float, float]]] = {}

    def on_reconnect(self) -> None:
        self._books.clear()

    def build_subscribe(self, symbols):
        # `sub.depth` is the live delta channel — ticks every ~100ms vs the
        # ~5s cadence of `sub.depth.full`. Snapshot arrives first, then
        # deltas; size=0 removes a level. limit=20 keeps payload small.
        return [
            {"method": "sub.depth", "param": {"symbol": f"{s}_USDT", "limit": 20}}
            for s in symbols
        ]

    def heartbeat_frame(self):
        return '{"method":"ping"}'

    def parse_message(self, msg):
        ch = msg.get("channel")
        if ch in ("pong", "rs.sub.depth", "rs.sub.depth.full"):
            return None
        if ch not in ("push.depth", "push.depth.full"):
            return None
        data = msg.get("data") or {}
        sym = msg.get("symbol") or ""
        if not sym.endswith("_USDT"):
            return None
        token = sym[:-5]
        book = self._books.setdefault(token, {"bids": {}, "asks": {}})
        # `push.depth.full` is a fresh snapshot (clear local first).
        if ch == "push.depth.full":
            book["bids"].clear()
            book["asks"].clear()
        # Apply (delta) entries: [price, qty, contract_count] — qty=0 removes.
        for side_key, raw in (("bids", data.get("bids") or []), ("asks", data.get("asks") or [])):
            store = book[side_key]
            for lvl in raw:
                try:
                    p = float(lvl[0]); q = float(lvl[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if q <= 0:
                    store.pop(p, None)
                else:
                    store[p] = q
        bids = sorted(book["bids"].items(), key=lambda x: -x[0])[:200]
        asks = sorted(book["asks"].items(), key=lambda x: x[0])[:200]
        return token, [[p, s] for p, s in bids], [[p, s] for p, s in asks]


# ── Whitebit Perp ─────────────────────────────────────────────────────────────
class WhitebitWS(WSAdapter):
    """WhiteBit perpetual depth.

    WhiteBit sends a full snapshot on subscribe (params[0]=True) and then
    incremental diffs (params[0]=False) that only carry changed levels.
    Previously we replaced the stored book with whatever arrived — which
    meant 80% of the levels vanished after the first diff. Now we keep a
    running {price → size} dict per symbol and merge deltas in place
    (size=0 means remove the level).
    """

    name = "whitebit"
    url = "wss://api.whitebit.com/ws"

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._books: dict[str, dict[str, dict[float, float]]] = {}

    def on_reconnect(self) -> None:
        # Drop local state so next subscribe's snapshot starts clean.
        self._books.clear()

    def build_subscribe(self, symbols):
        # 4th param = multiple_updates: first frame clears the server-side
        # subscription set, subsequent frames append. Depth=20 is enough
        # for top-of-book In/Out and ships at higher cadence than depth=100
        # (whitebit batches deeper books less frequently).
        return [
            {"id": i + 1, "method": "depth_subscribe",
             "params": [f"{s}_PERP", 20, "0", i > 0]}
            for i, s in enumerate(symbols)
        ]

    def parse_message(self, msg):
        if msg.get("method") != "depth_update":
            return None
        params = msg.get("params") or []
        if len(params) < 3:
            return None
        is_snapshot = bool(params[0])
        payload = params[1] or {}
        market = params[2] if len(params) > 2 else ""
        if not isinstance(market, str) or not market.endswith("_PERP"):
            return None
        token = market[:-5]

        book = self._books.setdefault(token, {"bids": {}, "asks": {}})
        if is_snapshot:
            book["bids"].clear()
            book["asks"].clear()

        for side_key in ("bids", "asks"):
            levels = payload.get(side_key) or []
            store = book[side_key]
            for lvl in levels:
                try:
                    price = float(lvl[0])
                    size = float(lvl[1])
                except (ValueError, IndexError, TypeError):
                    continue
                if size <= 0:
                    store.pop(price, None)
                else:
                    store[price] = size

        bids = sorted(book["bids"].items(), key=lambda x: -x[0])[:200]
        asks = sorted(book["asks"].items(), key=lambda x: x[0])[:200]
        return token, [[p, s] for p, s in bids], [[p, s] for p, s in asks]


# ── Hyperliquid ───────────────────────────────────────────────────────────────
class HyperliquidWS(WSAdapter):
    name = "hyperliquid"
    url = "wss://api.hyperliquid.xyz/ws"
    # Hyperliquid closes WS with 1011 after 60s without traffic. Their docs
    # spec'd app-level {"method":"ping"} → server sends {"channel":"pong"}.
    # WS-frame pings from the lib are ignored — disable them and use the
    # heartbeat hook instead. ~30s cadence.
    ping_interval = None  # type: ignore[assignment]
    ping_timeout = None   # type: ignore[assignment]

    def heartbeat_frame(self):
        return '{"method":"ping"}'

    def build_subscribe(self, symbols):
        return [
            {"method": "subscribe", "subscription": {"type": "l2Book", "coin": s}}
            for s in symbols
        ]

    def parse_message(self, msg):
        if msg.get("channel") != "l2Book":
            return None
        data = msg.get("data") or {}
        coin = data.get("coin")
        if not coin:
            return None
        levels = data.get("levels") or [[], []]
        bids_raw = [[x["px"], x["sz"]] for x in levels[0]]
        asks_raw = [[x["px"], x["sz"]] for x in levels[1]]
        bids, asks = _to_book(bids_raw, asks_raw)
        return coin, bids, asks


# ── KuCoin Futures (requires dynamic token) ──────────────────────────────────
class KuCoinWS(WSAdapter):
    name = "kucoin"
    # url is set dynamically from /api/v1/bullet-public
    url = ""
    ping_interval = 18.0
    subscribe_delay = 0.4  # KuCoin rate-limits subscribes to ~3/sec per connection
    max_symbols = 50        # >~100 topics per connection triggers ~90s server disconnect loop
    # Hot-spare second session: two independent WS connections, same symbol
    # set. When one drops, the other keeps streaming and arb compute never
    # sees a stale KuCoin orderbook. Each session has its own reconnect
    # backoff and connectId so they can't be disconnected by the same event.
    hot_spare: bool = True

    # Token cache — bullet-public tokens live >=24h per KuCoin docs. Reusing
    # the cached token on reconnect keeps outage windows <2 s; only the TCP
    # + WS handshake + subscribe is on the critical path.
    _cached_token: tuple[str, str, float, float] | None = None   # (endpoint, token, ping_s, fetched_at)
    _TOKEN_TTL = 21600.0   # 6 h

    async def _get_token(self, force: bool = False) -> tuple[str, str, float]:
        """Return (endpoint, token, ping_interval_s). Cached for _TOKEN_TTL."""
        import time as _t
        cached = KuCoinWS._cached_token
        if not force and cached and _t.time() - cached[3] < KuCoinWS._TOKEN_TTL:
            return cached[0], cached[1], cached[2]
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://api-futures.kucoin.com/api/v1/bullet-public")
            r.raise_for_status()
            d = r.json().get("data") or {}
            servers = d.get("instanceServers") or [{}]
            s = servers[0]
            endpoint = s.get("endpoint", "")
            token = d.get("token", "")
            ping_s = (s.get("pingInterval") or 18000) / 1000.0
            KuCoinWS._cached_token = (endpoint, token, ping_s, _t.time())
            return endpoint, token, ping_s

    def build_subscribe(self, symbols):
        # KuCoin supports comma-joined topics: "/contractMarket/level2Depth50:A,B,C"
        # One frame covers up to BATCH symbols, staying well below the 3/sec
        # subscribe-op rate limit even with many pairs.
        BATCH = 10
        frames = []
        mapped = [("XBT" if s == "BTC" else s) + "USDTM" for s in symbols]
        for i in range(0, len(mapped), BATCH):
            chunk = ",".join(mapped[i:i + BATCH])
            frames.append({
                "id": str(uuid.uuid4()),
                "type": "subscribe",
                "topic": f"/contractMarket/level2Depth50:{chunk}",
                "response": True,
            })
        return frames

    # Keep original per-symbol format disabled (stays here for reference)
    def _build_subscribe_single(self, symbols):
        frames = []
        for s in symbols:
            sym_k = ("XBT" if s == "BTC" else s) + "USDTM"
            frames.append({
                "id": str(uuid.uuid4()),
                "type": "subscribe",
                "topic": f"/contractMarket/level2Depth50:{sym_k}",
                "response": True,
            })
        return frames

    def heartbeat_frame(self):
        return '{"id":"ping","type":"ping"}'

    def parse_message(self, msg):
        if msg.get("type") in ("welcome", "ack", "pong"):
            return None
        if msg.get("type") != "message":
            return None
        topic = msg.get("topic", "")
        if not topic.startswith("/contractMarket/level2Depth50:"):
            return None
        sym_k = topic.split(":", 1)[1]  # e.g. XBTUSDTM
        if not sym_k.endswith("USDTM"):
            return None
        base = sym_k[:-5]
        token_sym = "BTC" if base == "XBT" else base
        data = msg.get("data") or {}
        bids, asks = _to_book(data.get("bids"), data.get("asks"))
        return token_sym, bids, asks

    async def _session(self, slot: str) -> None:
        """One WS session lifecycle. Runs forever, reconnects fast.
        `slot` is just a label ("A" or "B") for logs so we can tell
        primary vs hot-spare apart."""
        import websockets
        import json as _json
        import time as _time
        backoff = 0.3
        force_refresh_token = False
        fail_count = 0
        while not self._stop:
            hb_task = None
            try:
                try:
                    endpoint, token, ping_s = await self._get_token(force=force_refresh_token)
                except Exception as exc:
                    logger.warning("kucoin[%s] bullet-public failed: %s: %s",
                                   slot, type(exc).__name__, exc)
                    force_refresh_token = False
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.8, 8.0)
                    continue
                force_refresh_token = False
                if not endpoint or not token:
                    raise RuntimeError("kucoin bullet-public returned empty token")
                url = f"{endpoint}?token={token}&connectId={uuid.uuid4()}"
                async with websockets.connect(
                    url, ping_interval=None, ping_timeout=None,
                    open_timeout=30, close_timeout=3, max_size=4 * 1024 * 1024,
                ) as ws:
                    # Slot "A" owns the shared `_ws` pointer (used for manual
                    # closes from stop()). Slot "B" is the hot-spare.
                    if slot == "A":
                        self._ws = ws
                    backoff = 0.3
                    if slot == "A":
                        self._subscribed.clear()
                    try:
                        await ws.send(self.heartbeat_frame())
                    except Exception:
                        pass
                    if self._symbols:
                        # Each session subscribes independently.
                        frames = self.build_subscribe(list(self._symbols))
                        for f in frames:
                            try:
                                await ws.send(_json.dumps(f))
                            except Exception:
                                break
                            await asyncio.sleep(self.subscribe_delay)
                    # ping_s/3 instead of /2 — event loop starvation under
                    # heavy WS traffic can delay asyncio.sleep wakeups by
                    # 3-5 s, pushing our heartbeat past KuCoin's 18s cut-off
                    # and getting the session silently dropped.
                    hb_task = asyncio.create_task(self._heartbeat_loop(ws, ping_s / 3.0))
                    logger.info("kucoin[%s] WS connected (%d symbols)", slot, len(self._symbols))
                    fail_count = 0
                    # KuCoin server-side drops sessions at ~60s with no clean
                    # close frame. Pre-emptively rotate each slot before that:
                    # A at 45s, B at 55s so the two sessions never both hit a
                    # rotate at the same second. On rotate we break out of the
                    # recv loop; the outer while reconnects with jitter-free
                    # 0.3s backoff.
                    rotate_at = _time.time() + (45.0 if slot == "A" else 55.0)
                    async for raw in ws:
                        if self._stop:
                            break
                        if _time.time() > rotate_at:
                            logger.info("kucoin[%s] pre-emptive rotation", slot)
                            break
                        try:
                            msg = _json.loads(raw)
                        except Exception:
                            continue
                        parsed = None
                        try:
                            parsed = self.parse_message(msg)
                        except Exception:
                            continue
                        if parsed:
                            sym, b, a = parsed
                            if b or a:
                                self._update_cb(self.name, sym, b, a)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                import random as _r
                detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                fail_count += 1
                logger.warning("kucoin[%s] WS error (#%d): %s (retry in %.1fs)",
                               slot, fail_count, detail, backoff)
                msg = (str(exc) or "").lower()
                # Force fresh token on auth/token errors, OR after 3 handshake
                # timeouts in a row — the token may have been invalidated by
                # a server-side throttle without telling us.
                if ("401" in msg or "403" in msg or "invalid" in msg or
                    "expired" in msg or "token" in msg or fail_count >= 3):
                    force_refresh_token = True
                    fail_count = 0
                if slot == "A":
                    self._ws = None
                # Jitter ±30% so concurrent slots don't retry in lockstep.
                await asyncio.sleep(backoff * _r.uniform(0.7, 1.3))
                backoff = min(backoff * 1.8, 8.0)
            finally:
                if hb_task and not hb_task.done():
                    hb_task.cancel()
        if slot == "A":
            self._ws = None

    async def _run(self) -> None:
        """Run primary session. If hot_spare is enabled, also start a second
        session in parallel staggered by 15s so they don't hit KuCoin's
        rate-limit in the same window and can't be disconnected together."""
        if self.hot_spare:
            async def _spare():
                await asyncio.sleep(15.0)
                await self._session("B")
            spare_task = asyncio.create_task(_spare(), name="kucoin_ws_spare")
            try:
                await self._session("A")
            finally:
                spare_task.cancel()
        else:
            await self._session("A")


# ── Paradex Perp orderbook ──────────────────────────────────────────────────
class ParadexWS(WSAdapter):
    """Paradex Starknet perp. Public WS at `wss://ws.api.prod.paradex.trade/v1`.

    Channel naming: `order_book.{market}.snapshot@20@100ms` — 20 levels
    per side, 100 ms broadcast cadence. Messages carry either snapshot
    (`update_type=s`) or delta (`update_type=d`) arrays of
    `{side, price, size}` objects. We keep a running dict per symbol.
    """

    name = "paradex"
    url = "wss://ws.api.prod.paradex.trade/v1"
    ping_interval = 20.0

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._books: dict[str, dict[str, dict[float, float]]] = {}

    def on_reconnect(self) -> None:
        self._books.clear()

    def build_subscribe(self, symbols):
        # One subscribe frame per symbol (Paradex is JSON-RPC; most
        # implementations accept one id per request).
        return [
            {
                "jsonrpc": "2.0",
                "method":  "subscribe",
                "params":  {"channel": f"order_book.{s}-USD-PERP.snapshot@20@100ms"},
                "id":      i + 1,
            }
            for i, s in enumerate(symbols)
        ]

    def parse_message(self, msg):
        if msg.get("method") != "subscription":
            return None
        params = msg.get("params") or {}
        channel = params.get("channel") or ""
        # `order_book.{market}.snapshot@15@100ms` → extract market
        if not channel.startswith("order_book."):
            return None
        try:
            market = channel.split(".")[1]  # e.g. "BTC-USD-PERP"
        except IndexError:
            return None
        if not market.endswith("-USD-PERP"):
            return None
        base = market[:-len("-USD-PERP")]
        data = params.get("data") or {}
        update_type = data.get("update_type") or "s"
        book = self._books.setdefault(base, {"bids": {}, "asks": {}})
        if update_type == "s":
            book["bids"].clear()
            book["asks"].clear()
        for entry in (data.get("inserts") or []):
            try:
                price = float(entry["price"])
                size = float(entry["size"])
            except (KeyError, TypeError, ValueError):
                continue
            side = entry.get("side", "").upper()
            key = "bids" if side == "BUY" else "asks" if side == "SELL" else None
            if key is None:
                continue
            if size <= 0:
                book[key].pop(price, None)
            else:
                book[key][price] = size
        for entry in (data.get("deletes") or []):
            try:
                price = float(entry["price"])
            except (KeyError, TypeError, ValueError):
                continue
            side = entry.get("side", "").upper()
            key = "bids" if side == "BUY" else "asks" if side == "SELL" else None
            if key is not None:
                book[key].pop(price, None)
        bids = sorted(book["bids"].items(), key=lambda x: -x[0])[:15]
        asks = sorted(book["asks"].items(), key=lambda x: x[0])[:15]
        return base, [[p, s] for p, s in bids], [[p, s] for p, s in asks]


# ── Spot orderbook WS — for Spot/Short In/Out ───────────────────────────────
# Same wire format as the futures adapters on the big-3 venues, just pointing
# at the spot endpoints. Registered under distinct names so the in-memory
# _book_cache stores spot books keyed "binance_spot:BTC" etc. without
# colliding with the futures books.
class BinanceSpotWS(BinanceWS):
    name = "binance_spot"
    url = "wss://stream.binance.com:9443/ws"
    _rest_depth_url = "https://api.binance.com/api/v3/depth"


class BybitSpotWS(BybitWS):
    name = "bybit_spot"
    url = "wss://stream.bybit.com/v5/public/spot"


class GateSpotWS(WSAdapter):
    """Gate.io spot orderbook WS — `spot.order_book` channel, 20 levels
    per tick at 1000 ms cadence. Independent from futures `fx-ws.gateio.ws`;
    spot goes through `api.gateio.ws`."""
    name = "gate_spot"
    url = "wss://api.gateio.ws/ws/v4/"

    def build_subscribe(self, symbols):
        import time as _t
        return [{
            "time": int(_t.time()),
            "channel": "spot.order_book",
            "event": "subscribe",
            "payload": [f"{s}_USDT", "20", "1000ms"],
        } for s in symbols]

    def parse_message(self, msg):
        if msg.get("channel") != "spot.order_book":
            return None
        if msg.get("event") not in ("update", "all"):
            return None
        result = msg.get("result") or {}
        pair = result.get("s") or ""
        if not pair.endswith("_USDT"):
            return None
        token = pair[:-5]
        bids, asks = _to_book(result.get("bids"), result.get("asks"))
        return token, bids, asks


class BitgetSpotWS(WSAdapter):
    """Bitget V2 spot books — same channel name ('books') as futures but
    instType=SPOT. Full snapshot + delta protocol, need to maintain a local
    price→size dict to apply deltas (size='0' removes the level).

    Bitget V2 expects the CLIENT to send a literal `ping` text frame
    every 30 s; the server replies with `pong`. Without that the server
    closes the socket with "no close frame received" after ~25 s — what
    we were observing in production. heartbeat_frame() returns the raw
    `ping` text and the base loop fires it on the configured interval.
    """
    name = "bitget_spot"
    url = "wss://ws.bitget.com/v2/ws/public"
    ping_interval = 25.0  # under Bitget's 30 s server-side timeout

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._books: dict[str, dict[str, dict[float, float]]] = {}

    def on_reconnect(self) -> None:
        self._books.clear()

    def heartbeat_frame(self) -> str | None:
        return "ping"

    def build_subscribe(self, symbols):
        args = [{"instType": "SPOT", "channel": "books", "instId": f"{s}USDT"} for s in symbols]
        return {"op": "subscribe", "args": args}

    def parse_message(self, msg):
        if msg.get("event"):
            return None
        arg = msg.get("arg", {})
        if arg.get("channel") != "books" or arg.get("instType") != "SPOT":
            return None
        inst = arg.get("instId", "")
        if not inst.endswith("USDT"):
            return None
        token = inst[:-4]
        data_list = msg.get("data") or []
        if not data_list:
            return None
        data = data_list[0]
        action = msg.get("action") or "snapshot"
        book = self._books.setdefault(token, {"bids": {}, "asks": {}})
        if action == "snapshot":
            book["bids"].clear()
            book["asks"].clear()
        for side_key in ("bids", "asks"):
            for lvl in data.get(side_key) or []:
                try:
                    price = float(lvl[0])
                    size = float(lvl[1])
                except (ValueError, IndexError, TypeError):
                    continue
                if size <= 0:
                    book[side_key].pop(price, None)
                else:
                    book[side_key][price] = size
        bids = sorted(book["bids"].items(), key=lambda x: -x[0])[:200]
        asks = sorted(book["asks"].items(), key=lambda x: x[0])[:200]
        return token, [[p, s] for p, s in bids], [[p, s] for p, s in asks]


class BingXSpotWS(WSAdapter):
    """BingX spot: different host from futures (open-api-ws vs
    open-api-swap). `{pair}@depth20` gives a 20-level snapshot per tick.
    Frames are gzip-compressed — use the base adapter's decompress_gzip
    flag. No delta merging needed (snapshot-per-tick)."""
    name = "bingx_spot"
    url = "wss://open-api-ws.bingx.com/market"
    decompress_gzip = True

    def build_subscribe(self, symbols):
        return [
            {"id": str(i), "reqType": "sub", "dataType": f"{s}-USDT@depth20"}
            for i, s in enumerate(symbols)
        ]

    def parse_message(self, msg):
        dt = msg.get("dataType", "")
        if "@depth" not in dt:
            return None
        pair = dt.split("@")[0]
        if not pair.endswith("-USDT"):
            return None
        token = pair.split("-")[0]
        data = msg.get("data", {})
        bids, asks = _to_book(data.get("bids"), data.get("asks"))
        return token, bids, asks


class OKXSpotWS(OKXWS):
    name = "okx_spot"
    url = "wss://ws.okx.com:8443/ws/v5/public"

    def build_subscribe(self, symbols):
        # Spot: instId is "BTC-USDT" (no -SWAP suffix).
        args = [{"channel": "books", "instId": f"{s}-USDT"} for s in symbols]
        return {"op": "subscribe", "args": args}

    def parse_message(self, msg):
        if msg.get("event"):
            return None
        arg = msg.get("arg", {})
        if arg.get("channel") != "books":
            return None
        inst = arg.get("instId", "")
        if not inst.endswith("-USDT"):
            return None
        token = inst.split("-")[0]
        data_list = msg.get("data") or []
        if not data_list:
            return None
        data = data_list[0]
        action = msg.get("action") or data.get("action") or "snapshot"
        book = self._books.setdefault(token, {"bids": {}, "asks": {}})
        if action == "snapshot":
            book["bids"].clear()
            book["asks"].clear()
        for side_key in ("bids", "asks"):
            for lvl in data.get(side_key) or []:
                try:
                    price = float(lvl[0])
                    size = float(lvl[1])
                except (ValueError, IndexError, TypeError):
                    continue
                if size <= 0:
                    book[side_key].pop(price, None)
                else:
                    book[side_key][price] = size
        bids = sorted(book["bids"].items(), key=lambda x: -x[0])[:200]
        asks = sorted(book["asks"].items(), key=lambda x: x[0])[:200]
        return token, [[p, s] for p, s in bids], [[p, s] for p, s in asks]


# ── KuCoin Spot (token-auth bullet flow) ─────────────────────────────────────
class KuCoinSpotWS(WSAdapter):
    """KuCoin spot orderbook via the token-authed `bullet-public` flow.

    Boot sequence:
      1. POST /api/v1/bullet-public (no creds) → {token, instanceServers[].endpoint}
      2. Connect `<endpoint>?token=<t>&connectId=<n>`. Token TTL is 24h.
      3. Subscribe `/spotMarket/level2Depth50:<SYM>-USDT` — snapshot stream
         every push, no delta merging needed. Top 50 bids/asks per tick.
      4. Server PINGs every 18 s; respond with `{"id":"<ts>","type":"pong"}`.

    We re-mint the token on every reconnect so a stale token can't lock us
    out. Cost is one HTTP POST per (re)connect, negligible.
    """
    name = "kucoin_spot"

    # KuCoin spot caps at ~100 topics per connection in practice; we cap
    # below that to leave headroom for the prewarm rotation.
    max_symbols: int | None = 80
    # KuCoin requires the CLIENT to ping every `pingInterval` (default
    # 18 s) — we use 15 s to stay clear of jitter. Without this the
    # server closes the socket with no error message after ~20 s.
    ping_interval = 15.0
    ping_timeout = None  # type: ignore[assignment]  # KuCoin only does app-level

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._connect_id = 0

    def heartbeat_frame(self) -> str | None:
        # Client-initiated ping. KuCoin's docs say `{"id":"<int>","type":"ping"}`.
        return json.dumps({"id": "hb", "type": "ping"})

    async def get_url(self) -> str:
        import httpx
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post("https://api.kucoin.com/api/v1/bullet-public")
            r.raise_for_status()
            j = r.json()
        data = j.get("data") or {}
        instances = data.get("instanceServers") or []
        token = data.get("token")
        if not (token and instances):
            raise RuntimeError("kucoin bullet-public returned no token / endpoints")
        endpoint = instances[0].get("endpoint")
        self._connect_id += 1
        # connectId must be unique per session.
        return f"{endpoint}?token={token}&connectId=avalant-{self._connect_id}"

    def pong_for(self, msg):
        # KuCoin sends {"id":"<srvid>", "type":"ping"}; respond with
        # {"id":"<same>", "type":"pong"}.
        if isinstance(msg, dict) and msg.get("type") == "ping":
            return json.dumps({"id": msg.get("id") or "0", "type": "pong"})
        return None

    def build_subscribe(self, symbols):
        # 100-topic frame is fine, but we still split into chunks of 50 to
        # keep individual ack messages small.
        topics = [f"{s.upper()}-USDT" for s in symbols]
        frames = []
        chunk = 50
        for i in range(0, len(topics), chunk):
            frames.append({
                "id": str(int(__import__("time").time() * 1000)) + str(i),
                "type": "subscribe",
                "topic": "/spotMarket/level2Depth50:" + ",".join(topics[i:i + chunk]),
                "privateChannel": False,
                "response": True,
            })
        return frames

    def parse_message(self, msg):
        # ping/welcome/ack handled by base via pong_for().
        msg_type = msg.get("type")
        if msg_type != "message":
            return None
        topic = msg.get("topic", "")
        if not topic.startswith("/spotMarket/level2Depth50:"):
            return None
        sym_pair = topic.split(":", 1)[1]
        if not sym_pair.endswith("-USDT"):
            return None
        token = sym_pair.split("-")[0]
        data = msg.get("data", {})
        # Each frame carries its own complete top-50 snapshot.
        bids, asks = _to_book(data.get("bids"), data.get("asks"))
        if not bids and not asks:
            return None
        return token, bids, asks


# ── HTX Spot (Huobi) — gzip-compressed, per-symbol depth.step0 ───────────────
class HtxSpotWS(WSAdapter):
    """HTX (Huobi) spot books. WS frames are gzip-compressed JSON; the
    base adapter handles decompression via decompress_gzip=True.

    One topic per symbol — ``market.<sym>usdt.depth.step0`` gives a top-150
    snapshot every push (~100 ms). Server sends ``{"ping":<ts>}``; we must
    respond with ``{"pong":<ts>}`` or get disconnected.
    """
    name = "htx_spot"
    # Two known endpoints — AWS is the documented "international"
    # gateway that's reachable from European IPs without rate-limit
    # surprises, but it also has periodic regional outages where the
    # opening handshake just times out. Round-robin so a flaky AWS
    # host doesn't keep us offline indefinitely.
    _hosts = (
        "wss://api-aws.huobi.pro/ws",
        "wss://api.huobi.pro/ws",
    )
    url = _hosts[0]
    decompress_gzip = True
    subscribe_delay = 0.04  # 25 subs/sec is well under HTX's per-conn cap
    # HTX implements its own JSON ping/pong; the WebSocket-protocol-level
    # ping the websockets lib sends gets no response and HTX kills the
    # connection with 1003. Disable lib pings entirely.
    ping_interval = None  # type: ignore[assignment]
    ping_timeout = None   # type: ignore[assignment]

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._host_idx = 0

    async def get_url(self) -> str:
        host = self._hosts[self._host_idx % len(self._hosts)]
        # Rotate for the NEXT (re)connect so a hung host gets retired
        # for one cycle on every reconnection — eventual recovery
        # within `len(_hosts)` retries.
        self._host_idx += 1
        return host

    def build_subscribe(self, symbols):
        return [
            {"sub": f"market.{s.lower()}usdt.depth.step0", "id": f"avalant_{i}"}
            for i, s in enumerate(symbols)
        ]

    def pong_for(self, msg):
        ping_ts = msg.get("ping") if isinstance(msg, dict) else None
        if ping_ts is not None:
            return json.dumps({"pong": ping_ts})
        return None

    def parse_message(self, msg):
        ch = msg.get("ch", "")
        if not ch.startswith("market.") or not ch.endswith(".depth.step0"):
            return None
        # ch = "market.btcusdt.depth.step0" → token = BTC
        sym_part = ch.split(".")[1]
        if not sym_part.endswith("usdt"):
            return None
        token = sym_part[:-4].upper()
        tick = msg.get("tick", {}) or {}
        bids, asks = _to_book(tick.get("bids"), tick.get("asks"))
        if not bids and not asks:
            return None
        return token, bids, asks


# ── MEXC Spot (v3 API, JSON depth.v3) ────────────────────────────────────────
class MexcSpotWS(WSAdapter):
    """MEXC spot books via the v3 WS endpoint. Channel
    ``spot@public.limit.depth.v3.api@<SYM>USDT@20`` gives a top-20 snapshot
    every push.

    Avoid the new ``aggre.depth`` channel which forces protobuf encoding —
    the JSON-friendly ``limit.depth`` works without a protobuf dependency.
    """
    name = "mexc_spot"
    url = "wss://wbs-api.mexc.com/ws"
    subscribe_delay = 0.05

    def build_subscribe(self, symbols):
        # MEXC accepts 30 channels per SUBSCRIPTION frame, batch accordingly.
        chunk = 25
        frames = []
        topics = [f"spot@public.limit.depth.v3.api@{s.upper()}USDT@20" for s in symbols]
        for i in range(0, len(topics), chunk):
            frames.append({"method": "SUBSCRIPTION", "params": topics[i:i + chunk]})
        return frames

    def parse_message(self, msg):
        # MEXC server pings: payload is the literal string "PING" — base
        # adapter decodes JSON, so a non-JSON "PING" surfaces as a ValueError
        # in the parent and is already silently ignored. The library-level
        # websocket ping/pong keeps the session alive.
        ch = msg.get("c") or msg.get("channel") or ""
        if not ch.startswith("spot@public.limit.depth.v3.api@"):
            return None
        # Channel layout: 'spot@public.limit.depth.v3.api@BTCUSDT@20'
        # → split('@') = ['spot', 'public...api', 'BTCUSDT', '20']. The
        # symbol lives at index 2 (the 4th tag is the depth count, NOT a
        # token).
        try:
            sym = ch.split("@")[2]
        except IndexError:
            return None
        if not sym.endswith("USDT"):
            return None
        token = sym[:-4]
        data = msg.get("d") or msg.get("data") or {}
        bids = data.get("bids") or data.get("b") or []
        asks = data.get("asks") or data.get("a") or []
        # MEXC returns objects {"p": "<price>", "v": "<size>"} or arrays.
        def _norm(arr):
            out = []
            for x in arr:
                if isinstance(x, dict):
                    p = x.get("p") or x.get("price")
                    v = x.get("v") or x.get("vol") or x.get("quantity")
                else:
                    p, v = (x[0], x[1]) if isinstance(x, (list, tuple)) and len(x) >= 2 else (None, None)
                if p is None or v is None:
                    continue
                try:
                    out.append([float(p), float(v)])
                except (TypeError, ValueError):
                    continue
            return out
        bids = _norm(bids)
        asks = _norm(asks)
        if not bids and not asks:
            return None
        return token, bids, asks


class HtxWS(WSAdapter):
    """HTX (Huobi) USDT-margined linear-swap orderbook WS.

    Endpoint `wss://api.hbdm.com/linear-swap-ws` — gzipped JSON, app-level
    ping/pong (same protocol as HTX spot). step0 = full raw depth, no
    aggregation. Topic format: ``market.<SYM>-USDT.depth.step0``.
    """
    name = "htx"
    _hosts = (
        "wss://api.hbdm.com/linear-swap-ws",
    )
    url = _hosts[0]
    decompress_gzip = True
    subscribe_delay = 0.04
    ping_interval = None  # type: ignore[assignment]
    ping_timeout = None   # type: ignore[assignment]

    def build_subscribe(self, symbols):
        return [
            {"sub": f"market.{s.upper()}-USDT.depth.step0", "id": f"avalant_{i}"}
            for i, s in enumerate(symbols)
        ]

    def pong_for(self, msg):
        ping_ts = msg.get("ping") if isinstance(msg, dict) else None
        if ping_ts is not None:
            return json.dumps({"pong": ping_ts})
        return None

    def parse_message(self, msg):
        ch = msg.get("ch", "")
        if not ch.startswith("market.") or not ch.endswith(".depth.step0"):
            return None
        # ch = "market.BTC-USDT.depth.step0" → BTC
        sym_part = ch.split(".")[1]
        if not sym_part.endswith("-USDT"):
            return None
        token = sym_part[:-5].upper()
        tick = msg.get("tick", {}) or {}
        bids, asks = _to_book(tick.get("bids"), tick.get("asks"))
        if not bids and not asks:
            return None
        return token, bids, asks


ADAPTERS: dict[str, type[WSAdapter]] = {
    "binance":      BinanceWS,
    "bybit":        BybitWS,
    "okx":          OKXWS,
    "bitget":       BitgetWS,
    "bingx":        BingXWS,
    "aster":        AsterWS,
    "gate":         GateWS,
    "mexc":         MEXCWS,
    "whitebit":     WhitebitWS,
    "hyperliquid":  HyperliquidWS,
    "kucoin":       KuCoinWS,
    "paradex":      ParadexWS,
    "htx":          HtxWS,
    # extended/lighter/ethereal still REST-only:
    #   • extended uses per-market WS URLs — incompatible with the single-
    #     connection-per-exchange model in WSManager. Needs a per-symbol
    #     adapter spawn path before we can wire it in.
    #   • lighter publishes integer market IDs that change on listings; need
    #     a cold-start REST fetch of /api/v1/orderBooks to map id↔symbol
    #     before subscribe. Doable, but requires extending the adapter base
    #     to allow async pre-subscribe work.
    #   • ethereal — public WS endpoint + subscribe format unverified against
    #     their docs. Risk of silent reconnect-loop in prod, deferring.
    # All three keep working through the existing REST poller in
    # orderbook_cache._fetch_direct.
    # Spot — only on the big-3 venues for now (covers ~70-80% of spot-short
    # opp volume). Extending to the rest needs per-venue WS work (kucoin has
    # an odd token-auth flow, gate uses different sub format, etc.).
    "binance_spot": BinanceSpotWS,
    "bybit_spot":   BybitSpotWS,
    "okx_spot":     OKXSpotWS,
    "gate_spot":    GateSpotWS,
    "bitget_spot":  BitgetSpotWS,
    "bingx_spot":   BingXSpotWS,
    "kucoin_spot":  KuCoinSpotWS,
    "htx_spot":     HtxSpotWS,
    # NOTE on `mexc_spot`: WS handshake fails from Contabo's IP range
    # (wbs.mexc.com replies "Reason: Blocked!"). MexcSpotWS class stays
    # in the file for re-enablement when a residential proxy is wired —
    # not registered here so the manager doesn't burn reconnect cycles
    # on it. In/Out for MEXC spot falls back to the ticker-based basis.
}
