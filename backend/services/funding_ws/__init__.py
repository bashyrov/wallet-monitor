"""WebSocket funding-rate streams.

Replaces the REST polling in arbitrage_service._fetch_* for exchanges
that expose funding via WS. Polled REST is still kept as a fallback
and for exchanges we haven't wired up yet.

Public surface:
    start_funding_ws_manager() -> FundingWSManager
    get_funding_ws_manager() -> FundingWSManager | None
    stop_funding_ws_manager()
    get_ws_rows(exchange) -> list[dict] | None        # None = WS not healthy
    is_ws_funding_supported(exchange: str) -> bool
    ws_health() -> dict[str, dict]                    # per-exchange snapshot
"""
from .manager import (
    start_funding_ws_manager,
    get_funding_ws_manager,
    stop_funding_ws_manager,
    get_ws_rows,
    is_ws_funding_supported,
    ws_health,
    ADAPTERS,
)

__all__ = [
    "start_funding_ws_manager",
    "get_funding_ws_manager",
    "stop_funding_ws_manager",
    "get_ws_rows",
    "is_ws_funding_supported",
    "ws_health",
    "ADAPTERS",
]
