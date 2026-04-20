"""Per-exchange funding-rate WS adapters.

Each adapter pulls whatever broadcast channel a venue exposes that carries
(price, funding rate, next funding timestamp, 24h USD volume) for every
linear USDT-M perp. Some venues bundle everything into one channel (Bybit
tickers, Gate tickers, Hyperliquid webData2); others need us to compose
two streams — usually "mark-price + funding" plus "24h ticker volume".

Output row schema matches the REST fetchers in arbitrage_service so the
merged cache is a drop-in replacement.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import httpx

from .base import FundingWSAdapter

logger = logging.getLogger("avalant.funding_ws")

# SYNC HTTP client for REST back-stops, used exclusively from the
# dedicated thread pool below so the event loop is never blocked on
# network I/O or JSON decoding.
_rest_http = httpx.Client(
    timeout=httpx.Timeout(connect=4.0, read=8.0, write=4.0, pool=2.0),
    headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip, deflate"},
    follow_redirects=True,
    limits=httpx.Limits(max_connections=60, max_keepalive_connections=24, keepalive_expiry=30),
    http2=False,
)

# Dedicated thread pool so a busy default executor (FastAPI, other
# to_thread calls) can't queue our REST backstops. 16 workers > 11
# adapters → every adapter's refresh can always grab a worker without
# waiting.
import concurrent.futures as _cf
_rest_executor = _cf.ThreadPoolExecutor(max_workers=16, thread_name_prefix="funding-rest")


# Useful tick: for venues where the stream only emits mark price, we keep
# a background REST poll that refreshes the 24h volume (which moves
# slowly enough that 60s cadence is fine).
async def _periodic(coro_factory, interval: float):
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("_periodic task error: %s", exc)
        await asyncio.sleep(interval)


# ── Binance USDT-M Futures ────────────────────────────────────────────────────
class BinanceFundingWS(FundingWSAdapter):
    """Two broadcast streams combined on one connection:
      · !markPrice@arr@1s  → mark, funding rate, next funding time
      · !ticker@arr         → 24h quote volume
    Binance allows a combined stream URL with /stream?streams=a/b.
    """

    name = "binance"
    url = "wss://fstream.binance.com/stream?streams=!markPrice@arr@1s/!ticker@arr"

    def build_subscribe(self):
        return None  # streams are in the URL

    def parse_message(self, msg):
        data = msg.get("data")
        stream = msg.get("stream", "")
        if not data:
            return None
        if stream.startswith("!markPrice"):
            out = []
            for item in data if isinstance(data, list) else []:
                sym = item.get("s", "")
                if not sym.endswith("USDT"):
                    continue
                try:
                    out.append({
                        "symbol":     sym[:-4],
                        "price":      float(item.get("p") or 0),       # markPrice
                        "rate":       float(item.get("r") or 0),       # fundingRate
                        "next_ts":    int(item.get("T") or 0) // 1000, # ms → s
                        "interval_h": 8.0,                              # Binance standard
                    })
                except (ValueError, TypeError):
                    continue
            return out
        if stream.startswith("!ticker"):
            out = []
            for item in data if isinstance(data, list) else []:
                sym = item.get("s", "")
                if not sym.endswith("USDT"):
                    continue
                try:
                    out.append({
                        "symbol":     sym[:-4],
                        "volume_usd": float(item.get("q") or 0),  # 24h quote asset volume
                    })
                except (ValueError, TypeError):
                    continue
            return out
        return None


# ── Aster (Binance-compatible endpoint) ───────────────────────────────────────
class AsterFundingWS(BinanceFundingWS):
    name = "aster"
    url = "wss://fstream.asterdex.com/stream?streams=!markPrice@arr@1s/!ticker@arr"


# ── Bybit Linear Perp ─────────────────────────────────────────────────────────
class BybitFundingWS(FundingWSAdapter):
    """Bybit's `tickers.{symbol}` is the combined feed — lastPrice, fundingRate,
    nextFundingTime, volume24h, turnover24h in a single message. We subscribe
    to ALL linear tickers at connect by pulling the symbol list from a REST
    snapshot and sending `subscribe` frames in batches of 10.
    """

    name = "bybit"
    url = "wss://stream.bybit.com/v5/public/linear"
    rest_refresh_interval_s = 2.0

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._symbols: list[str] = []

    async def _load_symbols(self):
        """One-time REST call to enumerate linear USDT perps."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://api.bybit.com/v5/market/tickers?category=linear")
                r.raise_for_status()
                data = r.json().get("result", {}).get("list", [])
                self._symbols = [it["symbol"] for it in data if it.get("symbol", "").endswith("USDT")]
        except Exception as exc:
            logger.warning("bybit funding: could not enumerate symbols: %s", exc)
            self._symbols = []

    def build_subscribe(self):
        if not self._symbols:
            # Will be re-called on next connect after _load_symbols populates
            asyncio.create_task(self._load_symbols())
            return None
        args = [f"tickers.{s}" for s in self._symbols]
        frames = []
        for i in range(0, len(args), 10):
            frames.append({"op": "subscribe", "args": args[i:i + 10]})
        return frames

    def heartbeat_frame(self):
        return '{"op":"ping"}'

    def parse_message(self, msg):
        if msg.get("op") or msg.get("success") is not None:
            return None
        topic = msg.get("topic", "")
        if not topic.startswith("tickers."):
            return None
        data = msg.get("data") or {}
        sym_pair = data.get("symbol", "")
        if not sym_pair.endswith("USDT"):
            return None
        token = sym_pair[:-4]
        # Bybit pushes a full snapshot on subscribe, then PARTIAL updates
        # (only changed fields). Missing field must not overwrite the
        # carry-forward value in the manager — emit only what's present.
        row = {"symbol": token, "interval_h": 8.0}
        try:
            if data.get("lastPrice") is not None:
                row["price"] = float(data["lastPrice"])
            elif data.get("markPrice") is not None:
                row["price"] = float(data["markPrice"])
            if data.get("fundingRate") is not None:
                row["rate"] = float(data["fundingRate"])
            if data.get("nextFundingTime") is not None:
                row["next_ts"] = int(data["nextFundingTime"]) // 1000
            if data.get("turnover24h") is not None:
                row["volume_usd"] = float(data["turnover24h"])
        except (ValueError, TypeError):
            return None
        return row

    async def _run(self) -> None:
        # Eager symbol load before the first connect
        if not self._symbols:
            await self._load_symbols()
        await super()._run()

    def rest_refresh_sync(self) -> list[dict] | None:
        r = _rest_http.get("https://api.bybit.com/v5/market/tickers?category=linear")
        if r.status_code != 200:
            return None
        out: list[dict] = []
        for it in (r.json().get("result", {}).get("list") or []):
            sym = it.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            try:
                row = {
                    "symbol":     sym[:-4],
                    "price":      float(it.get("lastPrice") or it.get("markPrice") or 0) or None,
                    "rate":       float(it["fundingRate"]) if it.get("fundingRate") else None,
                    "next_ts":    int(it.get("nextFundingTime") or 0) // 1000 or None,
                    "interval_h": 8.0,
                    "volume_usd": float(it["turnover24h"]) if it.get("turnover24h") else None,
                }
                out.append(row)
            except (ValueError, TypeError):
                continue
        return out


# ── OKX Perp ──────────────────────────────────────────────────────────────────
class OKXFundingWS(FundingWSAdapter):
    """OKX splits price and funding into separate channels. Subscribe to
    `tickers` (all SWAPs — we filter USDT client-side) for price + volume,
    and `funding-rate` per-instId for rate + next-ts. OKX caps tickers
    batch at ~100 args per frame.
    """

    name = "okx"
    url = "wss://ws.okx.com:8443/ws/v5/public"
    rest_refresh_interval_s = 2.0

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._insts: list[str] = []
        self._ref_task: asyncio.Task | None = None

    async def _load_instruments(self):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
                r.raise_for_status()
                data = r.json().get("data", [])
                self._insts = [
                    it["instId"] for it in data
                    if it.get("instId", "").endswith("-USDT-SWAP") and it.get("state") == "live"
                ]
        except Exception as exc:
            logger.warning("okx funding: instrument list failed: %s", exc)
            self._insts = []

    def build_subscribe(self):
        if not self._insts:
            asyncio.create_task(self._load_instruments())
            return None
        args_tick = [{"channel": "tickers", "instId": i} for i in self._insts]
        args_fund = [{"channel": "funding-rate", "instId": i} for i in self._insts]
        frames = []
        # OKX: 480 args per subscribe frame
        for pack in (args_tick, args_fund):
            for i in range(0, len(pack), 200):
                frames.append({"op": "subscribe", "args": pack[i:i + 200]})
        return frames

    def parse_message(self, msg):
        if msg.get("event"):
            return None
        arg = msg.get("arg", {})
        ch = arg.get("channel")
        rows_in = msg.get("data") or []
        if not rows_in:
            return None
        out = []
        if ch == "tickers":
            for d in rows_in:
                inst = d.get("instId", "")
                if not inst.endswith("-USDT-SWAP"):
                    continue
                token = inst.split("-")[0]
                try:
                    # 24h quote volume = volCcy24h × last (OKX reports base volume)
                    last = float(d.get("last") or 0)
                    vol_base = float(d.get("volCcy24h") or 0)
                    out.append({
                        "symbol":     token,
                        "price":      last,
                        "volume_usd": vol_base * last,
                    })
                except (ValueError, TypeError):
                    continue
        elif ch == "funding-rate":
            for d in rows_in:
                inst = d.get("instId", "")
                if not inst.endswith("-USDT-SWAP"):
                    continue
                token = inst.split("-")[0]
                try:
                    # OKX fundingTime is string ms
                    next_ts = int(d.get("fundingTime") or 0) // 1000
                    rate = float(d.get("fundingRate") or 0)
                    out.append({
                        "symbol":     token,
                        "rate":       rate,
                        "next_ts":    next_ts,
                        "interval_h": 8.0,  # OKX varies — refined via REST if needed
                    })
                except (ValueError, TypeError):
                    continue
        return out

    async def _run(self) -> None:
        if not self._insts:
            await self._load_instruments()
        await super()._run()

    def rest_refresh_sync(self) -> list[dict] | None:
        r = _rest_http.get("https://www.okx.com/api/v5/market/tickers?instType=SWAP")
        if r.status_code != 200:
            return None
        out: list[dict] = []
        for d in (r.json().get("data") or []):
            inst = d.get("instId", "")
            if not inst.endswith("-USDT-SWAP"):
                continue
            token = inst.split("-")[0]
            try:
                last = float(d.get("last") or 0)
                vol_base = float(d.get("volCcy24h") or 0)
                row = {
                    "symbol":     token,
                    "price":      last or None,
                    "volume_usd": (vol_base * last) or None,
                }
                out.append(row)
            except (ValueError, TypeError):
                continue
        return out


# ── Gate.io Futures ───────────────────────────────────────────────────────────
class GateFundingWS(FundingWSAdapter):
    """Gate's futures.tickers pushes an ALL-SYMBOLS snapshot when subscribed
    with payload=["!all"]. Each row has mark_price, funding_rate,
    funding_next_apply, volume_24h_usd, volume_24h_quote.
    """

    name = "gate"
    url = "wss://fx-ws.gateio.ws/v4/ws/usdt"
    rest_refresh_interval_s = 2.0

    def build_subscribe(self):
        import time as _t
        return {
            "time": int(_t.time()),
            "channel": "futures.tickers",
            "event": "subscribe",
            "payload": ["!all"],
        }

    def parse_message(self, msg):
        if msg.get("channel") != "futures.tickers":
            return None
        if msg.get("event") not in ("all", "update"):
            return None
        result = msg.get("result")
        if not result:
            return None
        items = result if isinstance(result, list) else [result]
        out = []
        for d in items:
            contract = d.get("contract") or ""
            if not contract.endswith("_USDT"):
                continue
            token = contract[:-5]
            try:
                row = {
                    "symbol":     token,
                    "interval_h": 8.0,
                }
                if d.get("mark_price") is not None:
                    row["price"] = float(d.get("mark_price"))
                elif d.get("last") is not None:
                    row["price"] = float(d.get("last"))
                if d.get("funding_rate") is not None:
                    row["rate"] = float(d.get("funding_rate"))
                if d.get("funding_next_apply") is not None:
                    row["next_ts"] = int(d.get("funding_next_apply") or 0)
                # Gate exposes 24h volume in USDT as "volume_24h_usd"
                if d.get("volume_24h_usd") is not None:
                    row["volume_usd"] = float(d.get("volume_24h_usd"))
                elif d.get("volume_24h_quote") is not None:
                    row["volume_usd"] = float(d.get("volume_24h_quote"))
                out.append(row)
            except (ValueError, TypeError):
                continue
        return out

    def rest_refresh_sync(self) -> list[dict] | None:
        r = _rest_http.get("https://api.gateio.ws/api/v4/futures/usdt/tickers")
        if r.status_code != 200:
            return None
        out: list[dict] = []
        for d in (r.json() or []):
            contract = d.get("contract") or ""
            if not contract.endswith("_USDT"):
                continue
            try:
                row = {
                    "symbol":     contract[:-5],
                    "price":      float(d["mark_price"]) if d.get("mark_price") else (float(d["last"]) if d.get("last") else None),
                    "rate":       float(d["funding_rate"]) if d.get("funding_rate") is not None else None,
                    "next_ts":    int(d["funding_next_apply"]) if d.get("funding_next_apply") else None,
                    "volume_usd": float(d["volume_24h_settle"]) if d.get("volume_24h_settle") else (float(d["volume_24h_quote"]) if d.get("volume_24h_quote") else None),
                    "interval_h": 8.0,
                }
                out.append(row)
            except (ValueError, TypeError):
                continue
        return out


# ── KuCoin Futures ────────────────────────────────────────────────────────────
class KuCoinFundingWS(FundingWSAdapter):
    """KuCoin needs a bullet-public token and then subscribes to
    `/contract/instrument:{symbol}` per contract. Token cached 1h.
    """

    name = "kucoin"
    url = ""  # populated dynamically
    rest_refresh_interval_s = 2.0

    _cached_token: tuple | None = None
    _TOKEN_TTL = 3600.0

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._symbols: list[str] = []

    async def _get_token(self):
        cached = KuCoinFundingWS._cached_token
        if cached and time.time() - cached[3] < self._TOKEN_TTL:
            return cached
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://api-futures.kucoin.com/api/v1/bullet-public")
            r.raise_for_status()
            d = r.json().get("data") or {}
            servers = d.get("instanceServers") or [{}]
            s = servers[0]
            endpoint = s.get("endpoint", "")
            token = d.get("token", "")
            ping_s = (s.get("pingInterval") or 18000) / 1000.0
            tup = (endpoint, token, ping_s, time.time())
            KuCoinFundingWS._cached_token = tup
            return tup

    async def _load_symbols(self):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://api-futures.kucoin.com/api/v1/contracts/active")
                r.raise_for_status()
                data = r.json().get("data", [])
                self._symbols = [
                    it["symbol"] for it in data
                    if it.get("symbol", "").endswith("USDTM")
                ]
        except Exception as exc:
            logger.warning("kucoin funding: contract list failed: %s", exc)
            self._symbols = []

    def build_subscribe(self):
        if not self._symbols:
            asyncio.create_task(self._load_symbols())
            return None
        # Batch ~50 per topic (KuCoin allows comma-joined symbols)
        BATCH = 50
        frames = []
        for i in range(0, len(self._symbols), BATCH):
            chunk = ",".join(self._symbols[i:i + BATCH])
            frames.append({
                "id": str(uuid.uuid4()),
                "type": "subscribe",
                "topic": f"/contract/instrument:{chunk}",
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
        if not topic.startswith("/contract/instrument:"):
            return None
        sym_k = topic.split(":", 1)[1].split(",")[0]  # topic may list multiple, use subject
        subject = msg.get("subject", "")
        data = msg.get("data") or {}
        # Subject can be "mark.index.price" or "funding.rate"
        if not sym_k.endswith("USDTM"):
            return None
        base = sym_k[:-5]
        token = "BTC" if base == "XBT" else base
        try:
            if subject == "mark.index.price":
                return {
                    "symbol":     token,
                    "price":      float(data.get("markPrice") or 0),
                }
            if subject == "funding.rate":
                # KuCoin funding: granularity ms, predictedValue is the current rate
                pv = data.get("predictedValue") or data.get("value")
                gran_ms = int(data.get("granularity") or 0)
                interval_h = (gran_ms / 3_600_000) if gran_ms else 8.0
                row = {
                    "symbol":     token,
                    "interval_h": interval_h,
                }
                if pv is not None:
                    row["rate"] = float(pv)
                return row
        except (ValueError, TypeError):
            return None
        return None

    async def _run(self) -> None:
        if not self._symbols:
            await self._load_symbols()
        # KuCoin requires the token URL
        import random
        backoff = 1.0
        while not self._stop:
            try:
                endpoint, token, ping_s, _ = await self._get_token()
                connect_id = str(uuid.uuid4())
                self.url = f"{endpoint}?token={token}&connectId={connect_id}"
                self.ping_interval = ping_s / 2  # send pings at half interval
            except Exception as exc:
                logger.warning("kucoin funding: token fetch failed: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            try:
                await super()._run()
            except Exception as exc:
                logger.warning("kucoin funding inner loop: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            if self._stop:
                break

    def rest_refresh_sync(self) -> list[dict] | None:
        r = _rest_http.get("https://api-futures.kucoin.com/api/v1/contracts/active")
        if r.status_code != 200:
            return None
        out: list[dict] = []
        for d in (r.json().get("data") or []):
            sym = d.get("symbol", "")
            if not sym.endswith("USDTM"):
                continue
            base = sym[:-5]
            token = "BTC" if base == "XBT" else base
            try:
                gran_ms = int(d.get("fundingRateGranularity") or 0)
                interval_h = (gran_ms / 3_600_000) if gran_ms else 8.0
                row = {
                    "symbol":     token,
                    "price":      float(d["markPrice"]) if d.get("markPrice") else (float(d["lastTradePrice"]) if d.get("lastTradePrice") else None),
                    "rate":       float(d["fundingFeeRate"]) if d.get("fundingFeeRate") is not None else None,
                    "next_ts":    int(d["nextFundingRateTime"]) // 1000 + int(time.time()) if d.get("nextFundingRateTime") else None,
                    "volume_usd": float(d["turnoverOf24h"]) if d.get("turnoverOf24h") else None,
                    "interval_h": interval_h,
                }
                out.append(row)
            except (ValueError, TypeError):
                continue
        return out


# ── MEXC Futures ──────────────────────────────────────────────────────────────
class MexcFundingWS(FundingWSAdapter):
    """MEXC has a `sub.tickers` (ALL-symbols ticker broadcast) that provides
    lastPrice, fundingRate, volume24, amount24. One frame, everything.
    """

    name = "mexc"
    url = "wss://contract.mexc.com/edge"
    rest_refresh_interval_s = 2.0

    def build_subscribe(self):
        return {"method": "sub.tickers", "param": {}}

    def heartbeat_frame(self):
        return '{"method":"ping"}'

    def parse_message(self, msg):
        if msg.get("channel") in ("pong", "rs.sub.tickers"):
            return None
        if msg.get("channel") != "push.tickers":
            return None
        items = msg.get("data") or []
        out = []
        # MEXC push.tickers delivers price + volume only (no funding rate).
        # rest_refresh() fills rate + next_ts from /api/v1/contract/funding_rate
        # every 3s. We only surface what WS actually sent to avoid writing
        # stale/zero values onto carry-forward state.
        for d in items:
            sym = d.get("symbol", "")
            if not sym.endswith("_USDT"):
                continue
            token = sym[:-5]
            try:
                row = {"symbol": token, "interval_h": 8.0}
                if d.get("lastPrice") is not None:
                    row["price"] = float(d["lastPrice"])
                elif d.get("fairPrice") is not None:
                    row["price"] = float(d["fairPrice"])
                if d.get("amount24") is not None:
                    row["volume_usd"] = float(d["amount24"])
                out.append(row)
            except (ValueError, TypeError):
                continue
        return out

    def rest_refresh_sync(self) -> list[dict] | None:
        try:
            r_fr = _rest_http.get("https://contract.mexc.com/api/v1/contract/funding_rate")
        except Exception:
            r_fr = None
        try:
            r_tk = _rest_http.get("https://contract.mexc.com/api/v1/contract/ticker")
        except Exception:
            r_tk = None
        rate_map: dict[str, dict] = {}
        if r_fr is not None and r_fr.status_code == 200:
            for d in (r_fr.json().get("data") or []):
                sym = d.get("symbol", "")
                if not sym.endswith("_USDT"):
                    continue
                try:
                    coll_interval = d.get("collectCycle")
                    interval_h = float(coll_interval) if coll_interval else 8.0
                    rate_map[sym[:-5]] = {
                        "rate": float(d["fundingRate"]) if d.get("fundingRate") is not None else None,
                        "next_ts": int(d["nextSettleTime"]) // 1000 if d.get("nextSettleTime") else None,
                        "interval_h": interval_h,
                    }
                except (ValueError, TypeError):
                    continue
        out: list[dict] = []
        if r_tk is not None and r_tk.status_code == 200:
            for d in (r_tk.json().get("data") or []):
                sym = d.get("symbol", "")
                if not sym.endswith("_USDT"):
                    continue
                token = sym[:-5]
                try:
                    row = {
                        "symbol":     token,
                        "price":      float(d["lastPrice"]) if d.get("lastPrice") else None,
                        "volume_usd": float(d["amount24"]) if d.get("amount24") else None,
                        "interval_h": 8.0,
                    }
                    if token in rate_map:
                        row.update(rate_map[token])
                    out.append(row)
                except (ValueError, TypeError):
                    continue
        if not out and rate_map:
            out = [{"symbol": k, **v} for k, v in rate_map.items()]
        return out or None


# ── Bitget Perp ───────────────────────────────────────────────────────────────
class BitgetFundingWS(FundingWSAdapter):
    """Bitget V2 `ticker` channel per-symbol, instType=USDT-FUTURES. Carries
    last, fundingRate, nextFundingTime, usdtVolume. We pull the symbol list
    from the public contracts endpoint and subscribe in batches.
    """

    name = "bitget"
    url = "wss://ws.bitget.com/v2/ws/public"
    rest_refresh_interval_s = 2.0

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._symbols: list[str] = []

    async def _load_symbols(self):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://api.bitget.com/api/v2/mix/market/contracts?productType=USDT-FUTURES")
                r.raise_for_status()
                data = r.json().get("data", []) or []
                self._symbols = [
                    it.get("symbol", "") for it in data
                    if it.get("symbol", "").endswith("USDT")
                ]
        except Exception as exc:
            logger.warning("bitget funding: contract list failed: %s", exc)
            self._symbols = []

    def build_subscribe(self):
        if not self._symbols:
            asyncio.create_task(self._load_symbols())
            return None
        args = [{"instType": "USDT-FUTURES", "channel": "ticker", "instId": s} for s in self._symbols]
        frames = []
        # Bitget accepts large batches; 100 per frame is safe
        for i in range(0, len(args), 100):
            frames.append({"op": "subscribe", "args": args[i:i + 100]})
        return frames

    def heartbeat_frame(self):
        return "ping"  # Bitget uses plain-text ping

    def parse_message(self, msg):
        if msg == "pong" or msg.get("event"):
            return None
        arg = msg.get("arg", {})
        if arg.get("channel") != "ticker":
            return None
        items = msg.get("data") or []
        out = []
        for d in items:
            sym = d.get("instId", "")
            if not sym.endswith("USDT"):
                continue
            token = sym[:-4]
            try:
                out.append({
                    "symbol":     token,
                    "price":      float(d.get("lastPr") or d.get("markPr") or 0),
                    "rate":       float(d.get("fundingRate") or 0),
                    "next_ts":    int(d.get("nextFundingTime") or 0) // 1000,
                    "interval_h": 8.0,
                    "volume_usd": float(d.get("usdtVolume") or d.get("quoteVolume") or 0),
                })
            except (ValueError, TypeError):
                continue
        return out

    async def _run(self) -> None:
        if not self._symbols:
            await self._load_symbols()
        await super()._run()

    def rest_refresh_sync(self) -> list[dict] | None:
        r = _rest_http.get(
            "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"
        )
        if r.status_code != 200:
            return None
        out: list[dict] = []
        for d in (r.json().get("data") or []):
            sym = d.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            try:
                out.append({
                    "symbol":     sym[:-4],
                    "price":      float(d["lastPr"]) if d.get("lastPr") else (float(d["markPrice"]) if d.get("markPrice") else None),
                    "rate":       float(d["fundingRate"]) if d.get("fundingRate") is not None else None,
                    "next_ts":    int(d["nextFundingTime"]) // 1000 if d.get("nextFundingTime") else None,
                    "interval_h": 8.0,
                    "volume_usd": float(d["usdtVolume"]) if d.get("usdtVolume") else (float(d["quoteVolume"]) if d.get("quoteVolume") else None),
                })
            except (ValueError, TypeError):
                continue
        return out


# ── BingX Perp ────────────────────────────────────────────────────────────────
class BingXFundingWS(FundingWSAdapter):
    """BingX per-symbol `{symbol}@ticker` and `{symbol}@markPrice` — combined.
    BingX sends gzip-compressed frames and requires app-level Pong.
    """

    name = "bingx"
    url = "wss://open-api-swap.bingx.com/swap-market"
    decompress_gzip = True
    rest_refresh_interval_s = 2.0

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._symbols: list[str] = []

    async def _load_symbols(self):
        """Fetch all active USDT-M perps and sort them by 24h volume desc.
        The sort order only matters if we ever have to truncate (we don't
        today — all symbols are subscribed) but it's free insurance against
        a future hard ceiling from BingX: highest-volume pairs, which are
        the ones users actually arb, stay at the head of the list.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                # /contracts gives us the full active list; /ticker gives
                # 24h quote volume for ordering.
                contracts_r, ticker_r = await asyncio.gather(
                    c.get("https://open-api.bingx.com/openApi/swap/v2/quote/contracts"),
                    c.get("https://open-api.bingx.com/openApi/swap/v2/quote/ticker"),
                )
                contracts_r.raise_for_status()
                vol_map: dict[str, float] = {}
                if ticker_r.status_code == 200:
                    for t in (ticker_r.json().get("data") or []):
                        s = t.get("symbol", "")
                        try:
                            vol_map[s] = float(t.get("quoteVolume") or 0)
                        except (TypeError, ValueError):
                            pass
                syms = [
                    it.get("symbol", "")
                    for it in (contracts_r.json().get("data") or [])
                    if it.get("symbol", "").endswith("-USDT") and it.get("status") == 1
                ]
                # Volume-desc; unknown volume → end
                syms.sort(key=lambda s: vol_map.get(s, 0.0), reverse=True)
                self._symbols = syms
                logger.info("bingx funding: loaded %d symbols", len(syms))
        except Exception as exc:
            logger.warning("bingx funding: contract list failed: %s", exc)
            self._symbols = []

    def build_subscribe(self):
        if not self._symbols:
            asyncio.create_task(self._load_symbols())
            return None
        frames = []
        # Subscribe to every active USDT perp — previously capped at 500,
        # which silently froze price rows for ~150 mid-cap tokens (BSB, EDU,
        # PIEVERSE...) and leaked fake arb opps into the screener. BingX's
        # single-connection sub count is high enough to handle all ~650
        # contracts × 2 streams = ~1300 subs in one session.
        for i, s in enumerate(self._symbols):
            frames.append({"id": f"t-{i}", "reqType": "sub", "dataType": f"{s}@ticker"})
            frames.append({"id": f"m-{i}", "reqType": "sub", "dataType": f"{s}@markPrice"})
        return frames

    def parse_message(self, msg):
        dt = msg.get("dataType") or ""
        if msg.get("ping") is not None:
            return None  # BingX sends {"ping": ts}, websocket library replies automatically
        if "@ticker" not in dt and "@markPrice" not in dt:
            return None
        data = msg.get("data")
        if not data:
            return None
        pair = dt.split("@")[0]
        if not pair.endswith("-USDT"):
            return None
        token = pair[:-5]
        try:
            if "@ticker" in dt:
                return {
                    "symbol":     token,
                    "price":      float(data.get("c") or data.get("lastPrice") or 0),
                    "volume_usd": float(data.get("q") or data.get("turnover") or 0),
                }
            if "@markPrice" in dt:
                row = {
                    "symbol":     token,
                    "interval_h": 8.0,
                }
                if data.get("r") is not None:
                    row["rate"] = float(data.get("r"))
                if data.get("T") is not None:
                    row["next_ts"] = int(data.get("T") or 0) // 1000
                if data.get("p") is not None:
                    row["price"] = float(data.get("p"))
                return row
        except (ValueError, TypeError):
            return None
        return None

    async def _run(self) -> None:
        if not self._symbols:
            await self._load_symbols()
        await super()._run()

    def rest_refresh_sync(self) -> list[dict] | None:
        try:
            prem_r = _rest_http.get("https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex")
        except Exception:
            prem_r = None
        try:
            tick_r = _rest_http.get("https://open-api.bingx.com/openApi/swap/v2/quote/ticker")
        except Exception:
            tick_r = None
        vol_map: dict[str, tuple[float, float]] = {}
        if tick_r is not None and tick_r.status_code == 200:
            for t in (tick_r.json().get("data") or []):
                s = t.get("symbol", "")
                try:
                    vol_map[s] = (
                        float(t.get("lastPrice") or 0),
                        float(t.get("quoteVolume") or t.get("volume") or 0),
                    )
                except (TypeError, ValueError):
                    pass
        out: list[dict] = []
        if prem_r is not None and prem_r.status_code == 200:
            for d in (prem_r.json().get("data") or []):
                sym = d.get("symbol") or ""
                if not sym.endswith("-USDT"):
                    continue
                token = sym[:-5]
                try:
                    price, volume = vol_map.get(sym, (0.0, 0.0))
                    row = {
                        "symbol":     token,
                        "price":      price or (float(d["markPrice"]) if d.get("markPrice") else None),
                        "rate":       float(d["lastFundingRate"]) if d.get("lastFundingRate") is not None else None,
                        "next_ts":    int(d["nextFundingTime"]) // 1000 if d.get("nextFundingTime") else None,
                        "interval_h": float(d["fundingIntervalHours"]) if d.get("fundingIntervalHours") else 8.0,
                        "volume_usd": volume or None,
                    }
                    out.append(row)
                except (ValueError, TypeError):
                    continue
        return out or None


# ── WhiteBit Perp ─────────────────────────────────────────────────────────────
class WhitebitFundingWS(FundingWSAdapter):
    """WhiteBit `markets_subscribe` broadcasts all perp markets with price,
    funding rate, 24h volume. One subscribe, one stream.
    """

    name = "whitebit"
    url = "wss://api.whitebit.com/ws"

    def build_subscribe(self):
        # Subscribe to all perp markets; documentation says empty params = all
        return {"id": 1, "method": "markets_subscribe", "params": []}

    def parse_message(self, msg):
        method = msg.get("method")
        if method != "markets_update":
            return None
        params = msg.get("params") or []
        if len(params) < 2:
            return None
        payload = params[1] or {}
        out = []
        for market, info in payload.items():
            if not market.endswith("_PERP"):
                continue
            token = market[:-5]
            try:
                # WhiteBit market-update shape: {"last", "volume", "deal", "funding_rate"}
                row = {
                    "symbol":     token,
                    "interval_h": 8.0,
                }
                if info.get("last") is not None:
                    row["price"] = float(info.get("last"))
                if info.get("deal") is not None:
                    row["volume_usd"] = float(info.get("deal") or 0)
                if info.get("funding_rate") is not None:
                    row["rate"] = float(info.get("funding_rate"))
                out.append(row)
            except (ValueError, TypeError):
                continue
        return out


# ── Hyperliquid ───────────────────────────────────────────────────────────────
class HyperliquidFundingWS(FundingWSAdapter):
    """Hyperliquid `activeAssetCtx` subscription for each asset gives
    fundingRate, markPx, dayNtlVlm. Using `webData2` is richer but
    requires a user address. activeAssetCtx is public per coin.
    """

    name = "hyperliquid"
    url = "wss://api.hyperliquid.xyz/ws"

    def __init__(self, update_cb):
        super().__init__(update_cb)
        self._symbols: list[str] = []

    async def _load_symbols(self):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    "https://api.hyperliquid.xyz/info",
                    json={"type": "meta"},
                )
                r.raise_for_status()
                data = r.json() or {}
                universe = data.get("universe") or []
                # Only active, non-delisted perps — HL universe entries have "name"
                self._symbols = [a.get("name") for a in universe if a.get("name")]
        except Exception as exc:
            logger.warning("hyperliquid funding: universe fetch failed: %s", exc)
            self._symbols = []

    def build_subscribe(self):
        if not self._symbols:
            asyncio.create_task(self._load_symbols())
            return None
        frames = []
        for coin in self._symbols:
            frames.append({
                "method": "subscribe",
                "subscription": {"type": "activeAssetCtx", "coin": coin},
            })
        return frames

    def parse_message(self, msg):
        if msg.get("channel") != "activeAssetCtx":
            return None
        data = msg.get("data") or {}
        coin = data.get("coin")
        ctx = data.get("ctx") or {}
        if not coin:
            return None
        try:
            row = {
                "symbol":     coin,
                "interval_h": 1.0,  # Hyperliquid funds every hour
            }
            if ctx.get("markPx") is not None:
                row["price"] = float(ctx.get("markPx"))
            if ctx.get("funding") is not None:
                row["rate"] = float(ctx.get("funding"))
            if ctx.get("dayNtlVlm") is not None:
                row["volume_usd"] = float(ctx.get("dayNtlVlm"))
            return row
        except (ValueError, TypeError):
            return None

    async def _run(self) -> None:
        if not self._symbols:
            await self._load_symbols()
        await super()._run()


# ── Ethereal Perp ─────────────────────────────────────────────────────────────
# Ethereal doesn't expose a public ticker WS in their docs. Will stay on REST.


ADAPTERS: dict[str, type[FundingWSAdapter]] = {
    "binance":     BinanceFundingWS,
    "aster":       AsterFundingWS,
    "bybit":       BybitFundingWS,
    "okx":         OKXFundingWS,
    "gate":        GateFundingWS,
    "kucoin":      KuCoinFundingWS,
    "mexc":        MexcFundingWS,
    "bitget":      BitgetFundingWS,
    "bingx":       BingXFundingWS,
    "whitebit":    WhitebitFundingWS,
    "hyperliquid": HyperliquidFundingWS,
}
