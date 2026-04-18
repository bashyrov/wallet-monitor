"""Per-exchange WS adapter implementations."""
from __future__ import annotations

import logging

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


ADAPTERS: dict[str, type[WSAdapter]] = {
    "binance": BinanceWS,
    "bybit":   BybitWS,
    "okx":     OKXWS,
    "bitget":  BitgetWS,
    "bingx":   BingXWS,
}
