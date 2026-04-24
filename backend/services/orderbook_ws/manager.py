"""WebSocket orderbook manager.

Starts one WSAdapter instance per supported exchange on the owner worker.
Updates go into _book_cache (shared with REST pollers) so readers don't care
about the source. The owner worker also periodically dumps the cache to
/tmp/avalant_cache/books.json so other workers can read it.

Exchanges without WS support (perp DEXes, slow CEX) keep using the REST
poller path in orderbook_cache.py. Callers use is_ws_supported(ex) to decide.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .adapters import ADAPTERS
from .base import WSAdapter

logger = logging.getLogger("avalant.ws")


def is_ws_supported(exchange: str) -> bool:
    return exchange.lower() in ADAPTERS


_REDIS_MIN_INTERVAL_S = 0.05  # cap per-symbol Redis writes — 20 Hz is
# more than the 150 ms frontend poll; anything above is wasted Redis load.


class WSManager:
    def __init__(self):
        self._adapters: dict[str, WSAdapter] = {}
        self._last_redis_write: dict[str, float] = {}

    def _update_cb(self, exchange: str, symbol: str, bids: list, asks: list) -> None:
        """Called by each adapter on every incoming book update."""
        from backend.services.orderbook_cache import _book_cache
        key = f"{exchange}:{symbol}"
        entry = _book_cache.setdefault(key, {})
        now = time.time()
        entry["data"] = {"bids": bids, "asks": asks}
        entry["ts"] = now
        # keep last_request fresh so file-dumper includes it
        if "last_request" not in entry or now - entry.get("last_request", 0) > 5:
            entry["last_request"] = now
        # Publish to Redis at WS cadence — HTTP /orderbook no longer waits
        # for the 100-230 ms merger tick to propagate. Throttled per symbol
        # so bybit's 20 ms snapshot stream doesn't pound Redis at 12 K/s.
        last_r = self._last_redis_write.get(key, 0.0)
        if now - last_r >= _REDIS_MIN_INTERVAL_S:
            try:
                from backend.services.orderbook_redis import write_single
                write_single(key, {"ts": now, "data": {"bids": bids, "asks": asks}})
                self._last_redis_write[key] = now
            except Exception:
                pass

    def subscribe(self, exchange: str, symbols: list[str]) -> None:
        """Ensure an adapter for `exchange` is running and subscribed to `symbols`."""
        ex = exchange.lower()
        cls = ADAPTERS.get(ex)
        if not cls:
            return
        adapter = self._adapters.get(ex)
        if not adapter:
            adapter = cls(self._update_cb)
            self._adapters[ex] = adapter
            adapter.start(symbols)
        else:
            adapter.add_symbols(symbols)

    def set_symbols(self, exchange: str, symbols: list[str]) -> None:
        """Replace the exchange's subscription set — removes anything not in
        `symbols`, adds whatever's new. Used by the prewarm loop to rotate
        the hot list without accumulating forever."""
        ex = exchange.lower()
        cls = ADAPTERS.get(ex)
        if not cls:
            return
        adapter = self._adapters.get(ex)
        if not adapter:
            adapter = cls(self._update_cb)
            self._adapters[ex] = adapter
            adapter.start(symbols)
        else:
            adapter.set_symbols(symbols)

    def stop_all(self) -> None:
        for a in self._adapters.values():
            a.stop()
        self._adapters.clear()


_manager: WSManager | None = None


def start_ws_manager() -> WSManager:
    global _manager
    if _manager is None:
        _manager = WSManager()
        logger.info("WS manager started (supported: %s)", ", ".join(ADAPTERS))
    return _manager


def stop_ws_manager() -> None:
    global _manager
    if _manager:
        _manager.stop_all()
        _manager = None


def get_manager() -> WSManager | None:
    return _manager
