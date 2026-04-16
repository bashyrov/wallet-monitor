"""Per-exchange trade adapters.

Each adapter exposes the same async interface:
    await fetch_balance(creds) -> {"usdt": float}
    await set_leverage(creds, symbol, leverage, margin_mode) -> None
    await place_order(creds, symbol, side, quantity) -> {"order_id": str, "avg_price": float}
    await close_position(creds, symbol, side) -> {"order_id": str, "closed_qty": float, "realized_pnl_usd": float}
    await list_positions(creds, symbol?) -> [ {exchange, symbol, side, quantity, entry_price, unrealized_pnl_usd, ...} ]

Credentials dict is already decrypted before being passed in.
"""
from .binance import BinanceAdapter
from .bybit import BybitAdapter

ADAPTERS = {
    "binance": BinanceAdapter,
    "bybit":   BybitAdapter,
}

SUPPORTED_EXCHANGES = set(ADAPTERS.keys())
