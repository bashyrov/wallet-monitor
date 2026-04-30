"""WS user-stream framework.

One persistent WebSocket per (user, exchange-wallet) that pushes live
position / balance / order events. Replaces the 10s REST polling
loops in the trade panel.

Module layout:

  _base.py       — BaseUserStream interface + UserStreamEvent typing
  _supervisor.py — UserStreamSupervisor (lifecycle, state machine,
                   reconnect with parallel REST fallback)
  _snapshot.py   — Per-stream snapshot store (in-process + Redis mirror)
  <venue>.py     — One file per exchange (binance.py, aster.py, ...)

Adapters are registered in ADAPTERS so the supervisor can dispatch
without per-venue imports at call sites.
"""
from __future__ import annotations

from typing import Type

from backend.services.user_streams._base import BaseUserStream
from backend.services.user_streams.binance import BinanceUserStream
from backend.services.user_streams.aster import AsterUserStream
from backend.services.user_streams.bybit import BybitUserStream
from backend.services.user_streams.okx import OKXUserStream
from backend.services.user_streams.bitget import BitgetUserStream
from backend.services.user_streams.gate import GateUserStream
from backend.services.user_streams.bingx import BingXUserStream
from backend.services.user_streams.hyperliquid import HyperliquidUserStream
from backend.services.user_streams.kucoin import KuCoinUserStream
from backend.services.user_streams.mexc import MEXCUserStream
from backend.services.user_streams.backpack import BackpackUserStream
from backend.services.user_streams.lighter import LighterUserStream
from backend.services.user_streams.whitebit import WhitebitUserStream
from backend.services.user_streams.kraken import KrakenUserStream
from backend.services.user_streams.htx import HtxUserStream

ADAPTERS: dict[str, Type[BaseUserStream]] = {
    "binance": BinanceUserStream,
    "aster": AsterUserStream,
    "bybit": BybitUserStream,
    "okx": OKXUserStream,
    "bitget": BitgetUserStream,
    "gate": GateUserStream,
    "bingx": BingXUserStream,
    "hyperliquid": HyperliquidUserStream,
    "kucoin": KuCoinUserStream,
    "mexc": MEXCUserStream,
    "backpack": BackpackUserStream,
    "lighter": LighterUserStream,
    "whitebit": WhitebitUserStream,
    "kraken": KrakenUserStream,
    "htx": HtxUserStream,
}


def get_adapter(exchange: str) -> Type[BaseUserStream] | None:
    return ADAPTERS.get((exchange or "").lower())
