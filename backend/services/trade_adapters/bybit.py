"""Bybit v5 USDT perpetual trade adapter."""
from __future__ import annotations

import hashlib
import hmac
import json as jsonlib
import time
from typing import Any

import httpx

BASE = "https://api.bybit.com"


class BybitAdapter:
    @staticmethod
    def _sign(secret: str, api_key: str, timestamp: str, recv_window: str, query_or_body: str) -> str:
        pre = timestamp + api_key + recv_window + query_or_body
        return hmac.new(secret.encode(), pre.encode(), hashlib.sha256).hexdigest()

    @classmethod
    async def _signed(cls, creds: dict, method: str, path: str, params: dict | None = None) -> Any:
        params = params or {}
        ts = str(int(time.time() * 1000))
        recv = "5000"
        if method == "GET":
            q = "&".join(f"{k}={params[k]}" for k in sorted(params)) if params else ""
            sig = cls._sign(creds["api_secret"], creds["api_key"], ts, recv, q)
            headers = {
                "X-BAPI-API-KEY": creds["api_key"],
                "X-BAPI-SIGN": sig,
                "X-BAPI-SIGN-TYPE": "2",
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": recv,
            }
            url = BASE + path + ("?" + q if q else "")
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(url, headers=headers)
        else:
            body = jsonlib.dumps(params, separators=(",", ":"))
            sig = cls._sign(creds["api_secret"], creds["api_key"], ts, recv, body)
            headers = {
                "X-BAPI-API-KEY": creds["api_key"],
                "X-BAPI-SIGN": sig,
                "X-BAPI-SIGN-TYPE": "2",
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": recv,
                "Content-Type": "application/json",
            }
            url = BASE + path
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(url, headers=headers, content=body)
        if r.status_code >= 400:
            raise RuntimeError(f"Bybit {r.status_code}: {r.text}")
        j = r.json()
        if j.get("retCode") not in (0, None):
            raise RuntimeError(f"Bybit {j.get('retCode')}: {j.get('retMsg')}")
        return j.get("result") or {}

    @staticmethod
    def _symbol(s: str) -> str:
        return s.upper() + "USDT"

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        data = await cls._signed(creds, "GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        usdt = 0.0
        for row in data.get("list", []):
            for coin in row.get("coin", []):
                if coin.get("coin") == "USDT":
                    usdt = float(coin.get("availableToWithdraw") or coin.get("walletBalance") or 0)
                    break
        return {"usdt": usdt}

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        sym = cls._symbol(symbol)
        # Margin mode: Bybit uses setMarginMode (portfolio-wide for UTA) + set leverage per symbol
        try:
            await cls._signed(creds, "POST", "/v5/position/switch-isolated", {
                "category": "linear",
                "symbol": sym,
                "tradeMode": 1 if margin_mode == "isolated" else 0,
                "buyLeverage": str(int(leverage)),
                "sellLeverage": str(int(leverage)),
            })
        except RuntimeError as e:
            # 110026: already isolated/cross, 110043: leverage not modified
            if not any(code in str(e) for code in ("110026", "110043", "110027")):
                raise
        try:
            await cls._signed(creds, "POST", "/v5/position/set-leverage", {
                "category": "linear",
                "symbol": sym,
                "buyLeverage": str(int(leverage)),
                "sellLeverage": str(int(leverage)),
            })
        except RuntimeError as e:
            if "110043" not in str(e):
                raise

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float) -> dict:
        sym = cls._symbol(symbol)
        r = await cls._signed(creds, "POST", "/v5/order/create", {
            "category": "linear",
            "symbol": sym,
            "side": "Buy" if side == "buy" else "Sell",
            "orderType": "Market",
            "qty": f"{quantity:.6f}".rstrip('0').rstrip('.') or "0",
        })
        return {"order_id": str(r.get("orderId", "")), "avg_price": 0.0}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        sym = cls._symbol(symbol)
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        p = positions[0]
        reduce_side = "Sell" if p["side"] == "buy" else "Buy"
        r = await cls._signed(creds, "POST", "/v5/order/create", {
            "category": "linear",
            "symbol": sym,
            "side": reduce_side,
            "orderType": "Market",
            "qty": f"{p['quantity']:.6f}".rstrip('0').rstrip('.') or "0",
            "reduceOnly": True,
        })
        return {"order_id": str(r.get("orderId", "")), "closed_qty": p["quantity"], "realized_pnl_usd": p.get("unrealized_pnl_usd", 0)}

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        sym = cls._symbol(symbol)
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{BASE}/v5/market/instruments-info?category=linear&symbol={sym}")
                items = (r.json().get("result") or {}).get("list") or []
                if items:
                    ml = items[0].get("leverageFilter", {}).get("maxLeverage")
                    if ml:
                        return int(float(ml))
        except Exception:
            pass
        return 100

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        params = {"category": "linear"}
        if symbol:
            params["symbol"] = cls._symbol(symbol)
        else:
            params["settleCoin"] = "USDT"
        data = await cls._signed(creds, "GET", "/v5/position/list", params)
        out = []
        for p in data.get("list", []):
            qty = float(p.get("size") or 0)
            if qty == 0:
                continue
            side = "buy" if p.get("side") == "Buy" else "sell"
            out.append({
                "exchange": "bybit",
                "symbol": str(p.get("symbol", "")).replace("USDT", ""),
                "side": side,
                "quantity": qty,
                "entry_price": float(p.get("avgPrice") or 0),
                "mark_price":  float(p.get("markPrice") or 0),
                "unrealized_pnl_usd": float(p.get("unrealisedPnl") or 0),
                "leverage": int(float(p.get("leverage") or 1)),
                "position_id": str(p.get("symbol", "")),
            })
        return out
