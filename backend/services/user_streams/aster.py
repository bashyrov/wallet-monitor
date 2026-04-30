"""Aster user-stream — Binance fork, identical protocol with different
hostnames. We just override the REST/WS bases and inherit everything
else from BinanceUserStream.
"""
from __future__ import annotations

from backend.services.user_streams.binance import BinanceUserStream


class AsterUserStream(BinanceUserStream):
    name = "aster"
    rest_base = "https://fapi.asterdex.com"
    ws_base = "wss://fstream.asterdex.com/ws"
