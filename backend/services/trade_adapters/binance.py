"""Binance USDT-M Futures trade adapter (FAPI)."""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from typing import Any

import httpx

BASE = "https://fapi.binance.com"


class BinanceAdapter:
    @staticmethod
    def _sign(params: dict, secret: str) -> str:
        q = urllib.parse.urlencode(params, doseq=True)
        return hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()

    @classmethod
    async def _signed(cls, creds: dict, method: str, path: str, params: dict | None = None) -> Any:
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        sig = cls._sign(params, creds["api_secret"])
        params["signature"] = sig
        headers = {"X-MBX-APIKEY": creds["api_key"]}
        url = BASE + path
        async with httpx.AsyncClient(timeout=10) as c:
            if method == "GET":
                r = await c.get(url, params=params, headers=headers)
            elif method == "POST":
                r = await c.post(url, params=params, headers=headers)
            elif method == "DELETE":
                r = await c.delete(url, params=params, headers=headers)
            else:
                raise ValueError(method)
            if r.status_code >= 400:
                try:
                    raise RuntimeError(f"Binance {r.status_code}: {r.json().get('msg', r.text)}")
                except ValueError:
                    raise RuntimeError(f"Binance {r.status_code}: {r.text}")
            return r.json()

    # ── Public-ish helpers ────────────────────────────────────────────────
    @staticmethod
    def _symbol(s: str) -> str:
        return s.upper() + "USDT"

    # ── API ───────────────────────────────────────────────────────────────
    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        data = await cls._signed(creds, "GET", "/fapi/v2/balance")
        for x in data:
            if x.get("asset") == "USDT":
                return {"usdt": float(x.get("availableBalance", 0) or 0)}
        return {"usdt": 0.0}

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        sym = cls._symbol(symbol)
        try:
            await cls._signed(creds, "POST", "/fapi/v1/marginType",
                              {"symbol": sym, "marginType": "ISOLATED" if margin_mode == "isolated" else "CROSSED"})
        except RuntimeError as e:
            # -4046: No need to change margin type
            if "-4046" not in str(e) and "No need" not in str(e):
                raise
        await cls._signed(creds, "POST", "/fapi/v1/leverage",
                          {"symbol": sym, "leverage": int(leverage)})

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float) -> dict:
        sym = cls._symbol(symbol)
        r = await cls._signed(creds, "POST", "/fapi/v1/order", {
            "symbol": sym,
            "side": "BUY" if side == "buy" else "SELL",
            "type": "MARKET",
            "quantity": _round_qty(quantity),
        })
        return {"order_id": str(r.get("orderId")), "avg_price": float(r.get("avgPrice", 0) or 0)}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        # Close = reduce-only market order in the opposite direction
        sym = cls._symbol(symbol)
        # Fetch current position to know qty
        positions = await cls._signed(creds, "GET", "/fapi/v2/positionRisk", {"symbol": sym})
        qty_open = 0.0
        for p in positions:
            amt = float(p.get("positionAmt", 0) or 0)
            if amt != 0:
                qty_open = amt
                break
        if qty_open == 0:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        reduce_side = "SELL" if qty_open > 0 else "BUY"
        r = await cls._signed(creds, "POST", "/fapi/v1/order", {
            "symbol": sym,
            "side": reduce_side,
            "type": "MARKET",
            "quantity": _round_qty(abs(qty_open)),
            "reduceOnly": "true",
        })
        return {"order_id": str(r.get("orderId")), "closed_qty": abs(qty_open), "realized_pnl_usd": 0.0}

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        params = {"symbol": cls._symbol(symbol)} if symbol else None
        data = await cls._signed(creds, "GET", "/fapi/v2/positionRisk", params)
        out = []
        for p in data:
            amt = float(p.get("positionAmt", 0) or 0)
            if amt == 0:
                continue
            out.append({
                "exchange": "binance",
                "symbol": str(p.get("symbol", "")).replace("USDT", ""),
                "side":   "buy" if amt > 0 else "sell",
                "quantity": abs(amt),
                "entry_price": float(p.get("entryPrice", 0) or 0),
                "mark_price":  float(p.get("markPrice",  0) or 0),
                "unrealized_pnl_usd": float(p.get("unRealizedProfit", 0) or 0),
                "leverage": int(float(p.get("leverage", 1) or 1)),
                "position_id": str(p.get("symbol", "")),  # Binance doesn't expose a separate position ID
            })
        return out


def _round_qty(q: float) -> str:
    # Coarse rounding; exchange enforces its own stepSize which we don't fetch here
    # 6 decimals covers most USDT-M perpetuals; big-qty contracts will trim further
    return f"{q:.6f}".rstrip('0').rstrip('.') or "0"
