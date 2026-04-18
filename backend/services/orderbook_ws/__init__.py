"""WebSocket orderbook adapters — push-based, per-exchange.

Each adapter opens one persistent WS connection and streams top-N orderbook
snapshots. Every received message calls a shared update callback that writes
to the in-memory _book_cache in orderbook_cache.py (same structure used by
REST pollers so readers don't care about the source).

Adapters implemented: Binance, Bybit, OKX, Bitget, BingX. Everything else
(perp DEX + slow CEX like KuCoin/MEXC/Gate/Whitebit) stays on REST.
"""
from __future__ import annotations

from .manager import WSManager, start_ws_manager, stop_ws_manager, is_ws_supported

__all__ = ["WSManager", "start_ws_manager", "stop_ws_manager", "is_ws_supported"]
