"""Upbit trade adapter — spot-only (Upbit has no perpetuals).

Orders trade USDT-quoted markets ("USDT-BTC"). Upbit market orders are
asymmetric: BUY uses ord_type="price" with `price` = USDT amount to spend;
SELL uses ord_type="market" with `volume` = base quantity. Positions don't
exist on spot — list_positions is always empty and close_position sells
the wallet's base-currency balance.

Auth: JWT HS256 (access_key + nonce + SHA512 query_hash over the urlencoded
params) — same scheme for query params and POST bodies.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlencode

from backend.providers.exchanges.upbit_provider import UPBIT_BASE, upbit_auth_headers

logger = logging.getLogger("avalant.trade.upbit")


class UpbitAdapter:
    spot_native = True

    @staticmethod
    def _market(symbol: str) -> str:
        return f"USDT-{symbol.upper()}"

    @classmethod
    async def _request(cls, creds: dict, method: str, path: str, params: dict | None = None):
        import json as _j
        from backend.services.trade_adapters._http import http_client
        headers = upbit_auth_headers(creds["api_key"], creds["api_secret"], params)
        client = http_client(UPBIT_BASE, timeout=10.0)
        if method == "GET":
            rel = path + (f"?{urlencode(params, doseq=True)}" if params else "")
            r = await client.get(rel, headers=headers)
        else:
            headers["Content-Type"] = "application/json"
            r = await client.post(path, content=_j.dumps(params or {}), headers=headers)
        data = r.json()
        if r.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
            err = (data or {}).get("error") if isinstance(data, dict) else None
            msg = (err or {}).get("message") if isinstance(err, dict) else r.text[:200]
            raise RuntimeError(f"Upbit {r.status_code}: {msg}")
        return data

    @classmethod
    async def _last_price(cls, creds: dict, market: str) -> float:
        from backend.services.trade_adapters._http import http_client
        client = http_client(UPBIT_BASE, timeout=10.0)
        r = await client.get(f"/v1/ticker?markets={market}")
        rows = r.json() if r.status_code < 400 else []
        px = float((rows or [{}])[0].get("trade_price") or 0)
        if px <= 0:
            raise RuntimeError(f"Upbit: no price for {market}")
        return px

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        rows = await cls._request(creds, "GET", "/v1/accounts")
        spot_usd = 0.0
        for row in rows or []:
            if (row.get("currency") or "").upper() in ("USDT", "USDC"):
                try:
                    spot_usd += float(row.get("balance") or 0) + float(row.get("locked") or 0)
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
        market = cls._market(symbol)
        is_buy = (side or "").lower() in ("buy", "long")
        if is_buy:
            px = await cls._last_price(creds, market)
            params = {
                "market": market,
                "side": "bid",
                "ord_type": "price",
                "price": f"{float(quantity) * px:.8f}",
            }
        else:
            params = {
                "market": market,
                "side": "ask",
                "ord_type": "market",
                "volume": f"{float(quantity):.8f}",
            }
        data = await cls._request(creds, "POST", "/v1/orders", params)
        order_id = str(data.get("uuid") or "")
        avg_price, filled_qty = 0.0, 0.0
        try:
            await asyncio.sleep(0.4)
            od = await cls._request(creds, "GET", "/v1/order", {"uuid": order_id})
            trades = od.get("trades") or []
            spent = sum(float(t.get("funds") or 0) for t in trades)
            vol = sum(float(t.get("volume") or 0) for t in trades)
            if vol > 0:
                avg_price, filled_qty = spent / vol, vol
        except Exception as e:
            logger.debug("upbit order lookup %s: %s", order_id, e)
        return {"order_id": order_id, "avg_price": avg_price, "filled_qty": filled_qty or None}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        base = symbol.upper()
        rows = await cls._request(creds, "GET", "/v1/accounts")
        qty = 0.0
        for row in rows or []:
            if (row.get("currency") or "").upper() == base:
                try:
                    qty = float(row.get("balance") or 0)
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
