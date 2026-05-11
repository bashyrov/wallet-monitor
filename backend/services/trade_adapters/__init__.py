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
from .okx import OKXAdapter
from .gate import GateAdapter
from .mexc import MexcAdapter
from .kucoin import KuCoinAdapter
from .bitget import BitgetAdapter
from .bingx import BingxAdapter
from .whitebit import WhitebitAdapter
from .backpack import BackpackAdapter
from .hyperliquid import HyperliquidAdapter
from .aster import AsterAdapter
from .ethereal import EtherealAdapter
from .htx import HtxAdapter
from .lighter import LighterAdapter
from .kraken import KrakenAdapter
from .paradex import ParadexAdapter
from .readonly import make_readonly_adapter

# ── All exchanges/DEXes with full trade adapters ────────────────────────────
TRADE_SUPPORTED: set[str] = {
    # CEX (9)
    "binance", "bybit", "okx", "gate", "mexc", "kucoin", "bitget", "backpack",
    "kraken",
    # BingX + WhiteBIT
    "bingx", "whitebit",
    # Perp DEX (6) — require private key / API wallet / ZK key / Stark key
    "hyperliquid", "aster", "ethereal", "lighter", "paradex", "extended",
    # Spot-only — futures NOT implemented, leverage/close_position raise
    "htx",
}

ADAPTERS: dict[str, type] = {
    # CEX
    "binance":      BinanceAdapter,
    "bybit":        BybitAdapter,
    "okx":          OKXAdapter,
    "gate":         GateAdapter,
    "mexc":         MexcAdapter,
    "kucoin":       KuCoinAdapter,
    "bitget":       BitgetAdapter,
    "bingx":        BingxAdapter,
    "whitebit":     WhitebitAdapter,
    "backpack":     BackpackAdapter,
    "htx":          HtxAdapter,
    # Perp DEX
    "hyperliquid":  HyperliquidAdapter,
    "aster":        AsterAdapter,
    "ethereal":     EtherealAdapter,
    "lighter":      LighterAdapter,
    "kraken":       KrakenAdapter,
    "paradex":      ParadexAdapter,
}

# Extended is implemented in Go only (StarkEx Poseidon signing — no Python
# port). The dispatcher routes via trade_proxy when "extended" is in
# GO_TRADE_VENUES; this readonly stub satisfies the ADAPTERS registry +
# validate_key flow so users can add Extended keys from the wallet form.
_READONLY: dict[str, str] = {"extended": "Extended"}
for _key, _label in _READONLY.items():
    ADAPTERS[_key] = make_readonly_adapter(_key, _label)

SUPPORTED_EXCHANGES = set(ADAPTERS.keys())


# ── Runtime interface check — catches signature drift at import time ────────
from ._base import verify_adapter as _verify_adapter

for _name, _cls in ADAPTERS.items():
    if _name in _READONLY:
        continue  # readonly proxy is intentionally minimal
    try:
        _verify_adapter(_name, _cls)
    except ImportError as _exc:
        import logging as _logging
        _logging.getLogger("avalant.trade_adapters").error("adapter check: %s", _exc)
        # Don't block app start on a misbehaving adapter — just mark it
        # unsupported so trade_service rejects orders cleanly.
        SUPPORTED_EXCHANGES.discard(_name)

