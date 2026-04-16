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
from .readonly import make_readonly_adapter

# ── Trading-capable adapters ────────────────────────────────────────────────
TRADE_SUPPORTED: set[str] = {"binance", "bybit"}

# ── Read-only adapters for every other exchange/perp DEX we track ───────────
# Uses existing balance providers to validate keys; trading path raises.
_READONLY = {
    # CEX
    "okx":      "OKX",
    "gate":     "Gate",
    "kucoin":   "KuCoin",
    "mexc":     "MEXC",
    "bitget":   "Bitget",
    "backpack": "Backpack",
    # Perp DEX (these use the exchange provider registry via their own type,
    # so they work when type_value matches. For perpdex the wallet_type column
    # will be 'perpdex' — handled in trade_service filter separately.)
}

ADAPTERS: dict[str, type] = {
    "binance": BinanceAdapter,
    "bybit":   BybitAdapter,
}
for _key, _label in _READONLY.items():
    ADAPTERS[_key] = make_readonly_adapter(_key, _label)

SUPPORTED_EXCHANGES = set(ADAPTERS.keys())  # covers both trading + read-only

