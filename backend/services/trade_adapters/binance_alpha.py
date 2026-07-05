"""Binance Alpha trade adapter — spot-only.

An Alpha buy IS a Binance spot market order against USDT on the same
account, so this adapter reuses BinanceAdapter's signing and targets
api.binance.com. Positions don't exist on spot — close_position sells
the wallet's base-asset balance, list_positions is always empty.
"""
from __future__ import annotations

import logging
import time

from .binance import BinanceAdapter, _qty_to_str, _round_qty_to_step

logger = logging.getLogger("avalant.trade.binancealpha")

SPOT_BASE = "https://api.binance.com"

_info_cache: dict[str, tuple[dict, float]] = {}
_INFO_TTL_S = 600.0


async def _spot_filters(sym: str) -> dict:
    """LOT_SIZE step + derived precision from spot exchangeInfo, 10min cache."""
    hit = _info_cache.get(sym)
    if hit and (time.time() - hit[1]) < _INFO_TTL_S:
        return hit[0]
    from backend.services.trade_adapters._http import http_client
    client = http_client(SPOT_BASE, timeout=10.0)
    r = await client.get(f"/api/v3/exchangeInfo?symbol={sym}")
    out = {"step": 0.0, "precision": 8}
    if r.status_code < 400:
        for s in (r.json().get("symbols") or []):
            for f in s.get("filters") or []:
                if f.get("filterType") == "LOT_SIZE":
                    step_s = str(f.get("stepSize") or "0")
                    out["step"] = float(step_s)
                    if "." in step_s:
                        out["precision"] = len(step_s.rstrip("0").split(".")[1])
                    else:
                        out["precision"] = 0
        _info_cache[sym] = (out, time.time())
    return out


class BinanceAlphaAdapter:
    spot_native = True

    @staticmethod
    def _symbol(s: str) -> str:
        return s.upper() + "USDT"

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        data = await BinanceAdapter._signed(creds, "GET", "/api/v3/account", spot_host=SPOT_BASE)
        spot_usd = 0.0
        for b in data.get("balances") or []:
            if (b.get("asset") or "").upper() in ("USDT", "USDC"):
                try:
                    spot_usd += float(b.get("free") or 0) + float(b.get("locked") or 0)
                except (TypeError, ValueError):
                    pass
        return {"usdt": spot_usd, "spot_usd": spot_usd, "futures_usd": 0.0}

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        return None  # spot — no leverage

    @classmethod
    async def place_order(
        cls, creds: dict, symbol: str, side: str,
        quantity: float, leverage: int = 1, margin_mode: str = "isolated",
    ) -> dict:
        sym = cls._symbol(symbol)
        filters = await _spot_filters(sym)
        qty_r = _round_qty_to_step(quantity, filters["step"], filters["precision"])
        params = {
            "symbol": sym,
            "side": "BUY" if (side or "").lower() in ("buy", "long") else "SELL",
            "type": "MARKET",
            "quantity": _qty_to_str(qty_r, filters["precision"]),
        }
        r = await BinanceAdapter._signed(creds, "POST", "/api/v3/order", params, spot_host=SPOT_BASE)
        fills = r.get("fills") or []
        filled = sum(float(f.get("qty") or 0) for f in fills)
        quote = sum(float(f.get("qty") or 0) * float(f.get("price") or 0) for f in fills)
        avg = quote / filled if filled > 0 else 0.0
        return {"order_id": str(r.get("orderId") or ""), "avg_price": avg, "filled_qty": filled or None}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        base = symbol.upper()
        data = await BinanceAdapter._signed(creds, "GET", "/api/v3/account", spot_host=SPOT_BASE)
        qty = 0.0
        for b in data.get("balances") or []:
            if (b.get("asset") or "").upper() == base:
                try:
                    qty = float(b.get("free") or 0)
                except (TypeError, ValueError):
                    qty = 0.0
                break
        if qty <= 0:
            return {"order_id": "", "closed_qty": 0.0, "realized_pnl_usd": 0.0}
        result = await cls.place_order(creds, symbol, "sell", qty)
        return {
            "order_id": result.get("order_id") or "",
            "closed_qty": result.get("filled_qty") or qty,
            "realized_pnl_usd": 0.0,
            "avg_price": result.get("avg_price") or 0.0,
        }

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        return []  # spot — holdings live in the balance snapshot
