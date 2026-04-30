"""Base interface for per-venue user-stream adapters.

Each adapter must:
  · open(creds) → returns (ws_url, headers/auth_payload) async
  · subscribe(ws) — send any post-connect subscribe / login frames
  · parse_event(raw_msg) → UserStreamEvent | None — convert venue's
    push payload into a normalised event
  · keep_alive(creds) — periodic task (e.g. listenKey renewal). Spawned
    by the supervisor in parallel with the WS recv loop.

Adapters do NOT manage reconnect, backoff, or the snapshot — those
live in _supervisor.py + _snapshot.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional


# ── Event types ─────────────────────────────────────────────────────────────
# We only emit two kinds of events for now. Order-status events get rolled
# into POSITION_UPDATE because the user-facing panel cares about position
# state, not raw order objects. Order History tab is fed by trade_orders
# DB writes from place_open_order / close_position, not from the stream.
EVT_POSITION_UPDATE = "position_update"
EVT_BALANCE_UPDATE = "balance_update"


@dataclass
class UserStreamEvent:
    kind: str  # EVT_POSITION_UPDATE | EVT_BALANCE_UPDATE
    symbol: Optional[str] = None     # for POSITION_UPDATE; None for BALANCE_UPDATE
    side: Optional[str] = None       # buy | sell, None when position closed
    qty: float = 0.0
    entry_price: Optional[float] = None
    mark_price: Optional[float] = None
    unrealized_pnl_usd: Optional[float] = None
    leverage: Optional[int] = None
    margin_mode: Optional[str] = None
    balance_usdt: Optional[float] = None  # for BALANCE_UPDATE
    raw: dict = field(default_factory=dict)


class BaseUserStream:
    """Abstract — subclass per venue."""

    name: str = ""

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        """Return (ws_url, ws_headers). Some venues require a REST call
        (Binance listenKey, KuCoin bullet) before opening WS."""
        raise NotImplementedError

    @classmethod
    async def subscribe(cls, ws, creds: dict) -> None:
        """Send post-connect login / subscribe frames. Default: no-op
        (Binance listenKey URL already authenticates)."""
        return None

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        """Translate venue payload → UserStreamEvent. Return None for
        irrelevant frames (heartbeats, account-config notices, etc)."""
        raise NotImplementedError

    @classmethod
    async def keep_alive_loop(cls, creds: dict, stop_event) -> None:
        """Periodic task running alongside the WS recv loop. Used by
        Binance / Aster for listenKey PUT every 30 min. Default: no-op."""
        return None
