"""Ourbit spot trade adapter.

Ourbit is a spot-only exchange with a Binance v3-compatible REST surface.
Signing: HMAC-SHA256 over URL-encoded query string, signature appended as
`&signature=<hex>`, API key via `X-MBX-APIKEY` header.

Trade protocol notes:
  · fetch_balance  — USDT spot free balance from /api/v3/account.
  · place_order    — spot MARKET order on /api/v3/order.
                     side ∈ {buy, sell}; quantity is base-asset amount.
  · set_leverage   — Ourbit has no futures. Raises a clear error so the
                     UI explains rather than silently no-op'ing.
  · close_position — same; no concept of a futures position here.
  · list_positions — always returns []; spot balances are the only state.
"""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from backend.providers.exchanges._signing import hex_hmac_sha256

BASE = "https://api.ourbit.com"
logger = logging.getLogger("avalant.trade.ourbit")


class OurbitAdapter:
    @staticmethod
    def _symbol(s: str) -> str:
        return s.upper() + "USDT"

    @classmethod
    async def _signed(
        cls, creds: dict, method: str, path: str, params: dict | None = None
    ) -> Any:
        p: dict[str, Any] = dict(params or {})
        p["timestamp"] = str(int(time.time() * 1000))
        p.setdefault("recvWindow", "5000")
        qs = urlencode(p, doseq=True)
        sig = hex_hmac_sha256(creds["api_secret"], qs)
        url = f"{BASE}{path}?{qs}&signature={sig}"
        headers = {"X-MBX-APIKEY": creds["api_key"]}
        async with httpx.AsyncClient(timeout=10) as c:
            if method == "GET":
                r = await c.get(url, headers=headers)
            elif method == "POST":
                r = await c.post(url, headers=headers)
            elif method == "DELETE":
                r = await c.delete(url, headers=headers)
            else:
                raise ValueError(f"unsupported method {method}")
        if r.status_code >= 400:
            raise RuntimeError(f"Ourbit {r.status_code}: {r.text}")
        return r.json()

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        data = await cls._signed(creds, "GET", "/api/v3/account")
        for b in (data.get("balances") or []):
            if (b.get("asset") or "").upper() == "USDT":
                free = float(b.get("free") or 0)
                return {"usdt": free}
        return {"usdt": 0.0}

    @classmethod
    async def set_leverage(
        cls, creds: dict, symbol: str, leverage: int, margin_mode: str
    ) -> None:
        raise RuntimeError("Ourbit is spot-only — leverage / margin mode not supported.")

    @classmethod
    async def place_order(
        cls, creds: dict, symbol: str, side: str,
        quantity: float, leverage: int = 1, margin_mode: str = "isolated",
    ) -> dict:
        sym = cls._symbol(symbol)
        side_u = side.upper()
        if side_u not in ("BUY", "SELL"):
            raise ValueError(f"invalid side {side}")
        params = {
            "symbol": sym,
            "side": side_u,
            "type": "MARKET",
            "quantity": f"{quantity:.8f}".rstrip("0").rstrip("."),
        }
        data = await cls._signed(creds, "POST", "/api/v3/order", params)
        order_id = str(data.get("orderId") or "")
        # MARKET fills carry fills list with price per fill; compute avg.
        fills = data.get("fills") or []
        total_qty = 0.0
        total_quote = 0.0
        for f in fills:
            q = float(f.get("qty") or 0)
            p = float(f.get("price") or 0)
            total_qty += q
            total_quote += q * p
        avg_price = (total_quote / total_qty) if total_qty > 0 else float(data.get("price") or 0)
        return {"order_id": order_id, "avg_price": avg_price}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        # On a spot account the "close" of an open position means selling
        # the base asset back to USDT. Caller must pass the opposite side
        # and an explicit quantity via place_order — this adapter refuses
        # to guess the remaining base-asset balance from state.
        raise RuntimeError(
            "Ourbit is spot-only — use place_order(side='sell', quantity=…) "
            "to unwind a spot position."
        )

    @classmethod
    async def list_positions(
        cls, creds: dict, symbol: str | None = None
    ) -> list[dict]:
        # Spot has no positions — return empty. Keeps the trade protocol
        # uniform for callers that iterate ADAPTERS.
        return []
