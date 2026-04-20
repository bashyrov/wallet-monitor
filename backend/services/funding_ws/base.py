"""Base class for per-exchange funding-rate WS adapters.

Protocol: each adapter connects to one (or more) WebSocket endpoints,
subscribes to broadcast-style channels that carry funding rate + mark
price + next-funding-ts + 24h volume for every linear USDT-M perp, and
pushes normalised rows to the manager's update callback.

Normalised row schema — identical to what REST fetchers return in
arbitrage_service so downstream code doesn't need to branch:

    {
        "symbol":     "BTC",           # base, no "USDT" suffix
        "exchange":   "binance",
        "price":      50000.0,
        "rate":       0.0001,           # funding rate as decimal
        "next_ts":    1234567890,       # unix seconds, 0 if unknown
        "interval_h": 8.0,              # funding interval in hours
        "volume_usd": 1000000.0,        # 24h USD quote volume
    }

Adapters don't need to include "apr" or "cross_listed" — those are
computed downstream.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from abc import abstractmethod

import websockets

logger = logging.getLogger("avalant.funding_ws")


class FundingWSAdapter:
    """One instance per exchange. Holds one (or a small pool of) WS
    connections and maintains a {symbol → row} dict for the caller."""

    name: str = ""
    url: str = ""
    ping_interval: float = 20.0
    decompress_gzip: bool = False

    # How long we tolerate silence from the stream before marking the
    # adapter "stale" (arbitrage_service will fall back to REST).
    stale_after_s: float = 30.0

    def __init__(self, update_cb):
        """update_cb(exchange, symbol, row_dict) — manager-provided."""
        self._update_cb = update_cb
        self._rows: dict[str, dict] = {}       # symbol → latest row
        self._last_update_ts: float = 0.0
        self._task: asyncio.Task | None = None
        self._ws = None
        self._stop = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.create_task(self._run(), name=f"funding_ws_{self.name}")

    def stop(self) -> None:
        self._stop = True
        if self._ws:
            try:
                asyncio.create_task(self._ws.close())
            except Exception:
                pass
        if self._task and not self._task.done():
            self._task.cancel()

    # ── To override ───────────────────────────────────────────────────────

    @abstractmethod
    def build_subscribe(self) -> list[dict] | dict | None:
        """JSON frame(s) to send on connect to subscribe to the feeds.
        Return None if the URL already carries the subscription in-path
        (e.g. combined-stream URLs on Binance).
        """
        raise NotImplementedError

    @abstractmethod
    def parse_message(self, msg) -> list[dict] | dict | None:
        """Parse an incoming frame. Return:
            · a single row dict (single-symbol update)
            · a list of row dicts (batch update)
            · None (heartbeat / ack / irrelevant)
        Each row MUST include all the keys listed in the schema above.
        """
        raise NotImplementedError

    def heartbeat_frame(self) -> str | None:
        """Optional: app-level ping frame sent every `ping_interval` seconds.
        Return None to rely on websocket-level pings only.
        """
        return None

    # ── Public state accessors ────────────────────────────────────────────

    def rows(self) -> list[dict]:
        return list(self._rows.values())

    def health(self) -> dict:
        now = time.time()
        age = (now - self._last_update_ts) if self._last_update_ts else None
        return {
            "connected": self._ws is not None,
            "symbols":   len(self._rows),
            "last_age_s": None if age is None else round(age, 1),
            "healthy":   age is not None and age < self.stale_after_s,
        }

    # ── Core loop ─────────────────────────────────────────────────────────

    async def _heartbeat_loop(self, ws, interval: float) -> None:
        frame = self.heartbeat_frame()
        if frame is None:
            return
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send(frame)
        except Exception:
            pass

    async def _run(self) -> None:
        import random
        backoff = 1.0
        while not self._stop:
            hb_task: asyncio.Task | None = None
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_interval,
                    close_timeout=3,
                    open_timeout=20,
                    max_size=8 * 1024 * 1024,  # funding broadcasts can be large (500-row arrays)
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    subs = self.build_subscribe()
                    if subs is not None:
                        frames = subs if isinstance(subs, list) else [subs]
                        for f in frames:
                            try:
                                await ws.send(json.dumps(f))
                            except Exception as exc:
                                logger.warning("%s funding subscribe send failed: %s", self.name, exc)
                                break
                    if self.heartbeat_frame() is not None:
                        hb_task = asyncio.create_task(self._heartbeat_loop(ws, self.ping_interval))
                    logger.info("%s funding WS connected", self.name)

                    async for raw in ws:
                        if self._stop:
                            break
                        if self.decompress_gzip and isinstance(raw, bytes):
                            try:
                                raw = gzip.decompress(raw).decode("utf-8")
                            except Exception:
                                pass
                        if isinstance(raw, (bytes, str)) and raw in (b"Ping", "Ping"):
                            try:
                                await ws.send("Pong")
                            except Exception:
                                pass
                            continue
                        try:
                            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                        except (ValueError, TypeError):
                            continue
                        try:
                            parsed = self.parse_message(msg)
                        except Exception as exc:
                            logger.debug("%s funding parse error: %s", self.name, exc)
                            continue
                        if not parsed:
                            continue
                        items = parsed if isinstance(parsed, list) else [parsed]
                        changed = False
                        now = time.time()
                        for row in items:
                            if not isinstance(row, dict):
                                continue
                            sym = row.get("symbol")
                            if not sym:
                                continue
                            # Carry-forward missing keys from the previous state
                            # for the same symbol — some streams split price and
                            # funding into separate channels.
                            prev = self._rows.get(sym) or {}
                            merged = {**prev, **{k: v for k, v in row.items() if v is not None}}
                            merged["exchange"] = self.name
                            merged["symbol"] = sym
                            self._rows[sym] = merged
                            changed = True
                            try:
                                self._update_cb(self.name, sym, merged)
                            except Exception:
                                pass
                        if changed:
                            self._last_update_ts = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                jitter = random.uniform(0, 1.0)
                wait = backoff + jitter
                logger.warning("%s funding WS error: %s (retry in %.1fs)", self.name, exc, wait)
                self._ws = None
                if hb_task and not hb_task.done():
                    hb_task.cancel()
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 30.0)
            finally:
                self._ws = None
                if hb_task and not hb_task.done():
                    hb_task.cancel()
