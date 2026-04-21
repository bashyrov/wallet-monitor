"""Orderbook REST-backstop manager — spins up one adapter per exchange on
the prewarm owner worker, hands it the current hotlist of symbols every
tick (reuses the same list the prewarm WS code already builds).
"""
from __future__ import annotations

import logging
from typing import Callable

from .base import OrderbookRestBackstop
from .adapters import BACKSTOPS

logger = logging.getLogger("avalant.orderbook.rest")

_running: dict[str, OrderbookRestBackstop] = {}


def start_rest_backstops(exchanges: list[str] | None = None) -> None:
    """Start a REST backstop thread for each supported exchange. Safe to
    call multiple times — already-running adapters are left alone."""
    wanted = set(exchanges) if exchanges else set(BACKSTOPS.keys())
    for ex in wanted:
        cls = BACKSTOPS.get(ex)
        if not cls:
            continue
        if ex in _running:
            continue
        adapter = cls()
        adapter.start()
        _running[ex] = adapter
    if _running:
        logger.info(
            "orderbook REST backstops running: %s",
            ", ".join(sorted(_running.keys())),
        )


def stop_rest_backstops() -> None:
    for ex, adapter in list(_running.items()):
        try:
            adapter.stop()
        except Exception:
            pass
    _running.clear()


def set_backstop_symbols(exchange: str, symbols: list[str]) -> None:
    a = _running.get(exchange)
    if a is not None:
        a.set_symbols(symbols)


def rest_backstop_health() -> dict[str, dict]:
    return {ex: a.health() for ex, a in _running.items()}
