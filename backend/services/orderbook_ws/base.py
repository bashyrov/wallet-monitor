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
    # Give the server 3× the ping interval before considering the link dead.
    # Same fix as funding_ws/base.py — ping_timeout == ping_interval killed
    # otherwise-healthy sessions under traffic spikes with 1011 errors.
    ping_timeout: float = 60.0
    decompress_gzip: bool = False  # set True for exchanges that gzip WS frames (BingX)
    subscribe_delay: float = 0.0   # seconds between subscribe frames (exchanges with rate limits)
    max_symbols: int | None = None # cap total subscriptions per connection (None = unlimited)

    def __init__(self, update_cb):
        """update_cb(exchange: str, symbol: str, bids, asks) writes to _book_cache."""
        self._update_cb = update_cb
        self._symbols: set[str] = set()        # everything we want subscribed
        self._subscribed: set[str] = set()     # already sent a subscribe frame for
        self._sub_lock = asyncio.Lock()        # serialize concurrent _send_subscribe calls
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

    async def get_url(self) -> str:
        """Return the WS URL to connect to. Default: static `self.url`.
        Override for venues like KuCoin that mint a short-lived token URL
        via a REST call before each connect."""
        return self.url

    def pong_for(self, msg) -> str | None:
        """Synchronous app-level ping responder. Return the JSON/text to
        send back when `msg` is the venue's ping frame (HTX, KuCoin),
        else None to let the message flow into parse_message().
        The base run loop awaits the send before reading the next frame
        so we never race with the receive coroutine."""
        return None

    def on_reconnect(self) -> None:
        """Hook for adapters that maintain local book state — called when a
        fresh WS connection is opened, so the snapshot-+-delta stream starts
        from a clean slate instead of merging into a stale book."""
        pass

    # ── lifecycle ────────────────────────────────────────────────────────────
    def _apply_cap(self, symbols: set[str]) -> set[str]:
        """Honour max_symbols — keep first N in sorted order (stable across calls)."""
        if self.max_symbols and len(symbols) > self.max_symbols:
            return set(sorted(symbols)[:self.max_symbols])
        return symbols

    def start(self, symbols: list[str]) -> None:
        self._symbols = self._apply_cap({s.upper() for s in symbols})
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
        combined = self._symbols | {s.upper() for s in symbols}
        capped = self._apply_cap(combined)
        new = capped - self._symbols
        if not new:
            return
        self._symbols = capped
        if self._ws:
            asyncio.create_task(self._send_subscribe(only=new))

    def set_symbols(self, symbols: list[str]) -> None:
        """Replace the subscription set with exactly `symbols`. Used by the
        prewarm loop to keep the active set bounded — without this the hot
        list kept accumulating every symbol that was ever in the top-80,
        eventually driving each adapter past 150 topics and starving the
        event loop with heartbeat traffic.

        Symbols that are in `symbols` but not in the current set are queued
        for subscribe. Anything already in the current set but no longer
        wanted is dropped by forcing a clean reconnect — most exchanges
        don't expose a reliable unsubscribe frame for our batched topic
        formats, and reconnecting with only the new set is simpler than
        maintaining per-adapter unsubscribe logic.
        """
        desired = self._apply_cap({s.upper() for s in symbols})
        if desired == self._symbols:
            return
        removed = self._symbols - desired
        added   = desired - self._symbols
        self._symbols = desired
        if removed and self._ws:
            # Reset by reconnect: cancel current run task, _run will loop
            # back into connect and resubscribe from self._symbols.
            try:
                ws = self._ws
                self._ws = None
                asyncio.create_task(ws.close())
            except Exception:
                pass
            return
        if added and self._ws:
            asyncio.create_task(self._send_subscribe(only=added))

    async def _send_subscribe(self, only: set[str] | None = None) -> None:
        """Subscribe. If `only` given, send only for those symbols (delta)."""
        async with self._sub_lock:
            if not self._ws:
                return
            syms = sorted(only) if only else sorted(self._symbols - self._subscribed)
            if not syms:
                return
            frames = self.build_subscribe(syms)
            if isinstance(frames, dict):
                frames = [frames]
            for i, f in enumerate(frames):
                if not self._ws:
                    return  # connection dropped mid-subscribe
                try:
                    await self._ws.send(json.dumps(f))
                except Exception:
                    return  # socket died — let _run reconnect handle it
                if self.subscribe_delay > 0 and i < len(frames) - 1:
                    await asyncio.sleep(self.subscribe_delay)
            self._subscribed.update(syms)

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
                connect_url = await self.get_url()
                async with websockets.connect(
                    connect_url,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                    close_timeout=3,
                    # 30s tolerance: Aster/BingX can take 5-10s for TLS+WS upgrade
                    # when the event loop is saturated by spot/dex compute; the
                    # previous 20s tripped on most scheduler-contention windows.
                    open_timeout=30,
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    # Fresh connection — re-subscribe to everything we want
                    self._subscribed.clear()
                    self.on_reconnect()
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
                        # (Binance lowercase, KuCoin uppercase, Bitget v2
                        # mixed). Canonicalise so we always reply with the
                        # same case the venue used.
                        if isinstance(raw, (bytes, str)):
                            _r = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
                            _rl = _r.lower()
                            if _rl == "ping":
                                try:
                                    await ws.send("pong" if _r == _rl else "Pong")
                                except Exception:
                                    pass
                                continue
                            if _rl == "pong":
                                # Server's reply to our heartbeat — drop quietly.
                                continue
                        try:
                            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                        except (ValueError, TypeError):
                            continue
                        # App-level ping (HTX `{"ping":<ts>}`, KuCoin
                        # `{"type":"ping","id":...}`): respond inline so
                        # the venue doesn't time us out on its keepalive.
                        try:
                            pong = self.pong_for(msg)
                        except Exception:
                            pong = None
                        if pong:
                            try:
                                await ws.send(pong)
                            except Exception:
                                pass
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
