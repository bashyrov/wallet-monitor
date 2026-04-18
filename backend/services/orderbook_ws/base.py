"""Base class for per-exchange WebSocket orderbook adapters."""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
from abc import abstractmethod

import websockets

logger = logging.getLogger("avalant.ws")


class WSAdapter:
    """One persistent WS connection to a single exchange.

    Subclasses override `url`, `build_subscribe()`, and `parse_message()`.
    The framework handles connection lifecycle, reconnect backoff, and
    dispatching parsed book snapshots to the shared cache updater.
    """

    name: str = ""
    url: str = ""
    ping_interval: float = 20.0
    decompress_gzip: bool = False  # set True for exchanges that gzip WS frames (BingX)

    def __init__(self, update_cb):
        """update_cb(exchange: str, symbol: str, bids, asks) writes to _book_cache."""
        self._update_cb = update_cb
        self._symbols: set[str] = set()
        self._task: asyncio.Task | None = None
        self._ws = None
        self._stop = False

    # ── to override ──────────────────────────────────────────────────────────
    @abstractmethod
    def build_subscribe(self, symbols: list[str]) -> list[dict] | dict:
        """Return one or more JSON frames to send on connect to subscribe."""
        raise NotImplementedError

    @abstractmethod
    def parse_message(self, msg: dict) -> tuple[str, list, list] | None:
        """Parse an incoming message. Return (symbol, bids, asks) or None if
        the message is a heartbeat / subscription ack / irrelevant frame."""
        raise NotImplementedError

    # Optional: exchange-specific quirks
    def heartbeat_frame(self) -> str | None:
        """Return a text frame to send as heartbeat, or None for default ping."""
        return None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self, symbols: list[str]) -> None:
        self._symbols = {s.upper() for s in symbols}
        if self._task and not self._task.done():
            # Already running — trigger reconnect so we resubscribe with new set
            asyncio.create_task(self._resubscribe())
            return
        self._stop = False
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()

    def add_symbols(self, symbols: list[str]) -> None:
        new = {s.upper() for s in symbols} - self._symbols
        if not new:
            return
        self._symbols |= new
        asyncio.create_task(self._resubscribe())

    async def _resubscribe(self) -> None:
        if not self._ws:
            return
        try:
            await self._send_subscribe()
        except Exception as exc:
            logger.warning("%s resubscribe error: %s", self.name, exc)

    async def _send_subscribe(self) -> None:
        frames = self.build_subscribe(sorted(self._symbols))
        if isinstance(frames, dict):
            frames = [frames]
        for f in frames:
            await self._ws.send(json.dumps(f))

    async def _heartbeat_loop(self, ws, interval: float = 15.0) -> None:
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
                    open_timeout=20,      # allow slow exchanges to complete TLS + WS handshake
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    if self._symbols:
                        await self._send_subscribe()
                    if self.heartbeat_frame() is not None:
                        hb_task = asyncio.create_task(self._heartbeat_loop(ws))
                    logger.info("%s WS connected (%d symbols)", self.name, len(self._symbols))
                    async for raw in ws:
                        if self._stop:
                            break
                        if self.decompress_gzip and isinstance(raw, bytes):
                            try:
                                raw = gzip.decompress(raw).decode("utf-8")
                            except Exception:
                                pass
                        # Some exchanges use "Ping"/"Pong" plain-text frames
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
                            logger.debug("%s parse error: %s", self.name, exc)
                            continue
                        if not parsed:
                            continue
                        sym, bids, asks = parsed
                        if bids or asks:
                            self._update_cb(self.name, sym, bids, asks)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                jitter = random.uniform(0, 1.0)
                wait = backoff + jitter
                logger.warning("%s WS error: %s (retry in %.1fs)", self.name, exc, wait)
                self._ws = None
                await asyncio.sleep(wait)
                backoff = min(backoff * 1.8, 30.0)
            finally:
                if hb_task and not hb_task.done():
                    hb_task.cancel()
        self._ws = None
        logger.info("%s WS stopped", self.name)
