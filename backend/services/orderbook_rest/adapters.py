"""Per-exchange sync REST fetchers for orderbook snapshots.

Each class inherits `OrderbookRestBackstop` and overrides `fetch_sync(symbol)`.
Returns {"bids": [[price, size]...], "asks": [...]} or None on any error.
Never raises — the loop handles cadence & logging.

All adapters reuse the shared `_rest_http` client in base.py. Per-exchange
rate-limit notes live with the class.

The per-exchange body intentionally mirrors the async `_fetch_direct` in
orderbook_cache.py line-for-line — the URLs and parsing are identical.
The only thing we're trading is the event loop.
"""
from __future__ import annotations

import logging
from typing import Dict, Type

from .base import OrderbookRestBackstop, _rest_http

logger = logging.getLogger("avalant.orderbook.rest")


def _pairs(raw, price_key=0, size_key=1) -> list:
    out = []
    for x in raw or []:
        try:
            out.append([float(x[price_key]), float(x[size_key])])
        except (ValueError, TypeError, IndexError, KeyError):
            continue
    return out


# ── Binance Futures ─────────────────────────────────────────────────────────
class BinanceRest(OrderbookRestBackstop):
    name = "binance"
    # depth20 is 100-weight; budget allows ~60 symbols/s comfortably.
    interval_s = 1.0
    concurrency = 10

    def fetch_sync(self, symbol: str) -> dict | None:
        r = _rest_http.get(
            f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}USDT&limit=20"
        )
        if r.status_code != 200:
            return None
        d = r.json()
        return {"bids": _pairs(d.get("bids")), "asks": _pairs(d.get("asks"))}


# ── Bybit Linear Perp ────────────────────────────────────────────────────────
class BybitRest(OrderbookRestBackstop):
    name = "bybit"
    interval_s = 1.0
    concurrency = 10

    def fetch_sync(self, symbol: str) -> dict | None:
        r = _rest_http.get(
            f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={symbol}USDT&limit=50"
        )
        if r.status_code != 200:
            return None
        d = r.json().get("result") or {}
        return {"bids": _pairs(d.get("b")), "asks": _pairs(d.get("a"))}


# ── OKX Swap ─────────────────────────────────────────────────────────────────
class OKXRest(OrderbookRestBackstop):
    name = "okx"
    interval_s = 1.0
    concurrency = 8

    def fetch_sync(self, symbol: str) -> dict | None:
        r = _rest_http.get(
            f"https://www.okx.com/api/v5/market/books?instId={symbol}-USDT-SWAP&sz=50"
        )
        if r.status_code != 200:
            return None
        arr = r.json().get("data") or [{}]
        d = arr[0] if arr else {}
        return {"bids": _pairs(d.get("bids")), "asks": _pairs(d.get("asks"))}


# ── Gate Futures USDT ────────────────────────────────────────────────────────
# Gate's orderbook endpoint is visibly slow (~1s/call). 80 symbols × 1s /
# 12 workers = ~7s per cycle; cadence matches that.
class GateRest(OrderbookRestBackstop):
    name = "gate"
    interval_s = 3.0
    concurrency = 12

    def fetch_sync(self, symbol: str) -> dict | None:
        r = _rest_http.get(
            f"https://api.gateio.ws/api/v4/futures/usdt/order_book?contract={symbol}_USDT&limit=20"
        )
        if r.status_code != 200:
            return None
        d = r.json()
        bids = [[float(x["p"]), float(x["s"])] for x in d.get("bids") or []
                if isinstance(x, dict) and "p" in x and "s" in x]
        asks = [[float(x["p"]), float(x["s"])] for x in d.get("asks") or []
                if isinstance(x, dict) and "p" in x and "s" in x]
        return {"bids": bids, "asks": asks}


# ── KuCoin Futures ───────────────────────────────────────────────────────────
# Notes:
#   · XBT <-> BTC mapping (KuCoin names BTC perp XBTUSDTM).
#   · Public rate limit is ~30 req/s per IP on level2/depthN endpoints.
#     interval_s=2.0 + concurrency=6 keeps a 60-symbol cycle under that.
class KuCoinRest(OrderbookRestBackstop):
    name = "kucoin"
    interval_s = 2.0
    concurrency = 6

    def fetch_sync(self, symbol: str) -> dict | None:
        sym = ("XBT" if symbol == "BTC" else symbol) + "USDTM"
        r = _rest_http.get(
            f"https://api-futures.kucoin.com/api/v1/level2/depth20?symbol={sym}"
        )
        if r.status_code != 200:
            return None
        d = (r.json() or {}).get("data") or {}
        return {"bids": _pairs(d.get("bids")), "asks": _pairs(d.get("asks"))}


# ── MEXC Futures ─────────────────────────────────────────────────────────────
class MEXCRest(OrderbookRestBackstop):
    name = "mexc"
    interval_s = 1.0
    concurrency = 8

    def fetch_sync(self, symbol: str) -> dict | None:
        r = _rest_http.get(
            f"https://contract.mexc.com/api/v1/contract/depth/{symbol}_USDT?limit=20"
        )
        if r.status_code != 200:
            return None
        d = (r.json() or {}).get("data") or {}
        return {"bids": _pairs(d.get("bids")), "asks": _pairs(d.get("asks"))}


# ── Bitget Mix ───────────────────────────────────────────────────────────────
class BitgetRest(OrderbookRestBackstop):
    name = "bitget"
    interval_s = 1.0
    concurrency = 8

    def fetch_sync(self, symbol: str) -> dict | None:
        r = _rest_http.get(
            f"https://api.bitget.com/api/v2/mix/market/merge-depth?symbol={symbol}USDT&productType=USDT-FUTURES&limit=50"
        )
        if r.status_code != 200:
            return None
        d = (r.json() or {}).get("data") or {}
        return {"bids": _pairs(d.get("bids")), "asks": _pairs(d.get("asks"))}


# ── Aster (Binance-compatible) ───────────────────────────────────────────────
# Aster rate-limits aggressively on bursty load (3002 "request frequency
# exceeds limit"). Keep interval_s a touch higher and concurrency lower.
class AsterRest(OrderbookRestBackstop):
    name = "aster"
    interval_s = 1.5
    concurrency = 6

    def fetch_sync(self, symbol: str) -> dict | None:
        r = _rest_http.get(
            f"https://fapi.asterdex.com/fapi/v1/depth?symbol={symbol}USDT&limit=20"
        )
        if r.status_code != 200:
            return None
        d = r.json()
        return {"bids": _pairs(d.get("bids")), "asks": _pairs(d.get("asks"))}


# ── Hyperliquid ──────────────────────────────────────────────────────────────
# Public POST /info with Content-Type: application/json; native coin names
# (no USDT suffix — symbol IS the coin for HL).
class HyperliquidRest(OrderbookRestBackstop):
    name = "hyperliquid"
    interval_s = 1.0
    concurrency = 8

    def fetch_sync(self, symbol: str) -> dict | None:
        r = _rest_http.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "l2Book", "coin": symbol},
            headers={"Content-Type": "application/json"},
        )
        if r.status_code != 200:
            return None
        levels = (r.json() or {}).get("levels") or [[], []]
        if len(levels) < 2:
            return None
        bids = [[float(x["px"]), float(x["sz"])] for x in levels[0]
                if isinstance(x, dict) and "px" in x and "sz" in x]
        asks = [[float(x["px"]), float(x["sz"])] for x in levels[1]
                if isinstance(x, dict) and "px" in x and "sz" in x]
        return {"bids": bids, "asks": asks}


# ── BingX ─────────────────────────────────────────────────────────────────────
class BingXRest(OrderbookRestBackstop):
    name = "bingx"
    interval_s = 1.5  # BingX public is slow; give it more headroom
    concurrency = 6

    def fetch_sync(self, symbol: str) -> dict | None:
        r = _rest_http.get(
            f"https://open-api.bingx.com/openApi/swap/v2/quote/depth?symbol={symbol}-USDT&limit=50"
        )
        if r.status_code != 200:
            return None
        d = (r.json() or {}).get("data") or {}
        return {"bids": _pairs(d.get("bids")), "asks": _pairs(d.get("asks"))}


# ── WhiteBit ─────────────────────────────────────────────────────────────────
class WhiteBitRest(OrderbookRestBackstop):
    name = "whitebit"
    interval_s = 1.5  # WhiteBit orderbook endpoint is visibly slow
    concurrency = 6

    def fetch_sync(self, symbol: str) -> dict | None:
        r = _rest_http.get(
            f"https://whitebit.com/api/v4/public/orderbook/{symbol}_PERP?limit=100&level=2"
        )
        if r.status_code != 200:
            return None
        d = r.json() or {}
        return {"bids": _pairs(d.get("bids")), "asks": _pairs(d.get("asks"))}


BACKSTOPS: Dict[str, Type[OrderbookRestBackstop]] = {
    "binance":     BinanceRest,
    "bybit":       BybitRest,
    "okx":         OKXRest,
    "gate":        GateRest,
    "kucoin":      KuCoinRest,
    "mexc":        MEXCRest,
    "bitget":      BitgetRest,
    "aster":       AsterRest,
    "hyperliquid": HyperliquidRest,
    "bingx":       BingXRest,
    "whitebit":    WhiteBitRest,
}
