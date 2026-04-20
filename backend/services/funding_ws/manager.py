"""Funding WS manager — one instance per process, owns one adapter per
supported exchange.

Usage from the rest of the codebase:

    from backend.services import funding_ws

    funding_ws.start_funding_ws_manager()          # app startup
    rows = funding_ws.get_ws_rows("binance")       # None if stale / not healthy
    ok   = funding_ws.is_ws_funding_supported("okx")
    funding_ws.stop_funding_ws_manager()           # app shutdown
"""
from __future__ import annotations

import logging
import time

from .adapters import ADAPTERS
from .base import FundingWSAdapter

logger = logging.getLogger("avalant.funding_ws")


class FundingWSManager:
    def __init__(self):
        self._adapters: dict[str, FundingWSAdapter] = {}

    def _update_cb(self, exchange: str, symbol: str, row: dict) -> None:
        """Currently a no-op — adapters manage their own _rows dict. Kept as
        the hook surface in case we later want to fan out to Redis / pub-sub.
        """
        pass

    def start_all(self) -> None:
        for ex, cls in ADAPTERS.items():
            if ex not in self._adapters:
                a = cls(self._update_cb)
                self._adapters[ex] = a
                a.start()
        logger.info("funding WS manager started (%d adapters)", len(self._adapters))

    def stop_all(self) -> None:
        for a in self._adapters.values():
            a.stop()
        self._adapters.clear()

    def rows(self, exchange: str) -> list[dict] | None:
        a = self._adapters.get(exchange)
        if not a:
            return None
        if not a.health().get("healthy"):
            return None
        # Only return rows where the critical fields are populated — some
        # venues (KuCoin's funding.rate subject, BingX markPrice) don't
        # fire on every tick, so a "healthy" adapter might still be
        # missing funding rate on fresh symbols. In that case the caller
        # falls back to REST rather than serving a broken row.
        complete = [
            r for r in a.rows()
            if r.get("price") and r.get("rate") is not None
        ]
        if len(complete) < max(5, len(a.rows()) // 4):
            # Adapter running but not producing enough usable data
            return None
        return complete

    def raw_rows(self, exchange: str) -> list[dict] | None:
        """Rows regardless of health — useful for debug / admin metrics."""
        a = self._adapters.get(exchange)
        return a.rows() if a else None

    def health(self) -> dict[str, dict]:
        return {ex: a.health() for ex, a in self._adapters.items()}


_manager: FundingWSManager | None = None


def start_funding_ws_manager() -> FundingWSManager:
    global _manager
    if _manager is None:
        _manager = FundingWSManager()
        _manager.start_all()
    return _manager


def get_funding_ws_manager() -> FundingWSManager | None:
    return _manager


def stop_funding_ws_manager() -> None:
    global _manager
    if _manager is not None:
        _manager.stop_all()
        _manager = None


def get_ws_rows(exchange: str) -> list[dict] | None:
    """Rows for an exchange, or None if the WS is unhealthy / not started.

    Returned rows are already in the schema expected by arbitrage_service —
    the caller just needs to stamp `apr` + `cross_listed` and merge with
    other exchanges.
    """
    if _manager is None:
        return None
    return _manager.rows(exchange)


def is_ws_funding_supported(exchange: str) -> bool:
    return exchange in ADAPTERS


def ws_health() -> dict[str, dict]:
    return _manager.health() if _manager else {}
