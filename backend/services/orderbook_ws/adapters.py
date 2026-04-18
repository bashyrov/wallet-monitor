"""Per-exchange WS adapter implementations."""
from __future__ import annotations

import asyncio
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


# ── Binance Futures ───────────────────────────────────────────────────────────
class BinanceWS(WSAdapter):
    name = "binance"
    url = "wss://fstream.binance.com/ws"

    def build_subscribe(self, symbols):
        # partial book 20 levels every 100ms
        params = [f"{s.lower()}usdt@depth20@100ms" for s in symbols]
        return {"method": "SUBSCRIBE", "params": params, "id": 1}

    def parse_message(self, msg):
        if msg.get("result") is None and "params" in msg:
            # subscription ack has "result": null — skip here
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

    def __init__(self, update_cb):
        super().__init__(update_cb)
        # Bybit sends snapshot then deltas — maintain local book per symbol
        self._books: dict[str, dict[str, dict]] = {}  # sym → {"bids": {p:q}, "asks": {p:q}}

    def build_subscribe(self, symbols):
        args = [f"orderbook.50.{s}USDT" for s in symbols]
        # Bybit limits 10 topics per subscribe frame
        frames = []
        for i in range(0, len(args), 10):
            frames.append({"op": "subscribe", "args": args[i:i + 10]})
        return frames

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


# ── OKX Perp (books5 — top-5, pushed on change) ───────────────────────────────
class OKXWS(WSAdapter):
    name = "okx"
    url = "wss://ws.okx.com:8443/ws/v5/public"

    def build_subscribe(self, symbols):
        args = [{"channel": "books5", "instId": f"{s}-USDT-SWAP"} for s in symbols]
        # OKX allows large subscribe batches
        return {"op": "subscribe", "args": args}

    def parse_message(self, msg):
        if msg.get("event"):
            return None
        arg = msg.get("arg", {})
        if arg.get("channel") != "books5":
            return None
        inst = arg.get("instId", "")
        if not inst.endswith("-USDT-SWAP"):
            return None
        token = inst.split("-")[0]
        data = (msg.get("data") or [{}])[0]
        bids, asks = _to_book(data.get("bids"), data.get("asks"))
        return token, bids, asks


# ── Bitget Perp (books15 snapshot) ────────────────────────────────────────────
class BitgetWS(WSAdapter):
    name = "bitget"
    url = "wss://ws.bitget.com/v2/ws/public"

    def build_subscribe(self, symbols):
        args = [{"instType": "USDT-FUTURES", "channel": "books15", "instId": f"{s}USDT"} for s in symbols]
        return {"op": "subscribe", "args": args}

    def parse_message(self, msg):
        if msg.get("event"):
            return None
        arg = msg.get("arg", {})
        if arg.get("channel") != "books15":
            return None
        inst = arg.get("instId", "")
        if not inst.endswith("USDT"):
            return None
        token = inst[:-4]
        data = (msg.get("data") or [{}])[0]
        bids, asks = _to_book(data.get("bids"), data.get("asks"))
        return token, bids, asks


# ── BingX Perp (depth20 on new URL) ───────────────────────────────────────────
class BingXWS(WSAdapter):
    name = "bingx"
    url = "wss://open-api-swap.bingx.com/swap-market"
    decompress_gzip = True

    def build_subscribe(self, symbols):
        # Each subscribe creates one subscription; BingX supports bulk via multiple frames
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

    def build_subscribe(self, symbols):
        import time as _t
        frames = []
        for s in symbols:
            frames.append({
                "time": int(_t.time()),
                "channel": "futures.order_book",
                "event": "subscribe",
                "payload": [f"{s}_USDT", "10", "0"],
            })
        return frames

    def parse_message(self, msg):
        if msg.get("channel") != "futures.order_book":
            return None
        if msg.get("event") != "all":
            return None
        result = msg.get("result") or {}
        contract = result.get("contract") or result.get("s") or ""
        if not contract.endswith("_USDT"):
            return None
        token = contract[:-5]
        raw_bids = [[x["p"], x["s"]] for x in result.get("bids", [])]
        raw_asks = [[x["p"], x["s"]] for x in result.get("asks", [])]
        bids, asks = _to_book(raw_bids, raw_asks)
        return token, bids, asks


# ── MEXC Futures ──────────────────────────────────────────────────────────────
class MEXCWS(WSAdapter):
    name = "mexc"
    url = "wss://contract.mexc.com/edge"

    def build_subscribe(self, symbols):
        return [
            {"method": "sub.depth.full", "param": {"symbol": f"{s}_USDT", "limit": 20}}
            for s in symbols
        ]

    def heartbeat_frame(self):
        # MEXC needs an app-level ping every 15s
        return '{"method":"ping"}'

    def parse_message(self, msg):
        if msg.get("channel") in ("pong", "rs.sub.depth.full"):
            return None
        if msg.get("channel") != "push.depth.full":
            return None
        data = msg.get("data") or {}
        sym = msg.get("symbol") or ""
        if not sym.endswith("_USDT"):
            return None
        token = sym[:-5]
        # MEXC uses [price, quantity, contract_count]
        raw_bids = [[x[0], x[1]] for x in data.get("bids", [])]
        raw_asks = [[x[0], x[1]] for x in data.get("asks", [])]
        bids, asks = _to_book(raw_bids, raw_asks)
        return token, bids, asks


# ── Whitebit Perp ─────────────────────────────────────────────────────────────
class WhitebitWS(WSAdapter):
    name = "whitebit"
    url = "wss://api.whitebit.com/ws"

    def build_subscribe(self, symbols):
        # multi=True so successive subscribes accumulate; first clears any prior
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
        payload = params[1] if not params[0] else params[1]
        market = params[2] if len(params) > 2 else ""
        if not isinstance(market, str) or not market.endswith("_PERP"):
            return None
        token = market[:-5]
        bids, asks = _to_book(payload.get("bids"), payload.get("asks"))
        return token, bids, asks


# ── Hyperliquid ───────────────────────────────────────────────────────────────
class HyperliquidWS(WSAdapter):
    name = "hyperliquid"
    url = "wss://api.hyperliquid.xyz/ws"

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

    async def _get_token(self) -> tuple[str, str, float]:
        """Return (endpoint, token, ping_interval_s) from bullet-public."""
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://api-futures.kucoin.com/api/v1/bullet-public")
            r.raise_for_status()
            d = r.json().get("data") or {}
            servers = d.get("instanceServers") or [{}]
            s = servers[0]
            return s.get("endpoint", ""), d.get("token", ""), (s.get("pingInterval") or 18000) / 1000.0

    def build_subscribe(self, symbols):
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

    async def _run(self) -> None:
        # Override to fetch token before each connection
        import websockets
        import json as _json
        backoff = 1.0
        while not self._stop:
            hb_task = None
            try:
                endpoint, token, ping_s = await self._get_token()
                if not endpoint or not token:
                    raise RuntimeError("kucoin bullet-public returned empty token")
                url = f"{endpoint}?token={token}&connectId={uuid.uuid4()}"
                # KuCoin does NOT respond to RFC-6455 control-frame pings —
                # only the app-level {"type":"ping"} JSON heartbeat. Disable
                # the websockets client's protocol ping so it doesn't fire a
                # ping_timeout and close the connection.
                async with websockets.connect(
                    url, ping_interval=None, ping_timeout=None,
                    open_timeout=20, close_timeout=3, max_size=4 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    if self._symbols:
                        await self._send_subscribe()
                    hb_task = asyncio.create_task(self._heartbeat_loop(ws, ping_s * 0.8))
                    logger.info("kucoin WS connected (%d symbols)", len(self._symbols))
                    async for raw in ws:
                        if self._stop:
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
                detail = str(exc) or type(exc).__name__
                logger.warning("kucoin WS error: %s (retry in %.1fs)", detail, backoff)
                self._ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.8, 30.0)
            finally:
                if hb_task and not hb_task.done():
                    hb_task.cancel()
        self._ws = None


ADAPTERS: dict[str, type[WSAdapter]] = {
    "binance":     BinanceWS,
    "bybit":       BybitWS,
    "okx":         OKXWS,
    "bitget":      BitgetWS,
    "bingx":       BingXWS,
    "aster":       AsterWS,
    "gate":        GateWS,
    "mexc":        MEXCWS,
    "whitebit":    WhitebitWS,
    "hyperliquid": HyperliquidWS,
    "kucoin":      KuCoinWS,
}
