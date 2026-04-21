"""Orderbook REST backstop.

Each exchange has an `OrderbookRestBackstop` subclass running in a pure
daemon thread. Polls the exchange's depth endpoint for top-N symbols
every `interval_s` and writes snapshots into `orderbook_cache._book_cache`.

This mirrors the funding_ws REST backstop (PR #20) — it protects the
orderbook data plane against WS drops, keepalive timeouts, and subscribe
races by guaranteeing a baseline ~1s freshness even when every WS session
is down. When WS is healthy it silently adds redundancy; when WS is sick
it becomes the primary source and the arb compute keeps working.
"""
from .base import OrderbookRestBackstop
from .adapters import BACKSTOPS
from .manager import (
    start_rest_backstops,
    stop_rest_backstops,
    set_backstop_symbols,
    rest_backstop_health,
)

__all__ = [
    "OrderbookRestBackstop",
    "BACKSTOPS",
    "start_rest_backstops",
    "stop_rest_backstops",
    "set_backstop_symbols",
    "rest_backstop_health",
]
