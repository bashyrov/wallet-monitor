"""Aster DEX trade adapter — EIP-712 API wallet signing.

Users create an API wallet at asterdex.com/en/api-wallet.
  creds = {"api_key": "0x... (main wallet)", "api_secret": "0x... (API signer private key)"}

Signing: EIP-712 typed data, Binance-like FAPI endpoint structure.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from typing import Any

import httpx

logger = logging.getLogger("avalant.trade.aster")

BASE = "https://fapi.asterdex.com"

_INSTR_CACHE: dict[str, tuple[dict, float]] = {}
_INSTR_TTL = 600
_INSTR_LOCK = asyncio.Lock()


class AsterAdapter:

    @staticmethod
    def _symbol(s: str) -> str:
        return s.upper() + "USDT"

    @classmethod
    async def _signed(cls, creds: dict, method: str, path: str, params: dict | None = None) -> Any:
        """EIP-712 signing — same as Aster's API wallet auth."""
        try:
            from eth_account import Account
            from eth_account.messages import encode_typed_data
        except ImportError:
            raise RuntimeError("eth_account package required for Aster trading")

        params = dict(params or {})
        params["timestamp"] = str(int(time.time() * 1e6))  # microseconds
        params["recvWindow"] = "5000"

        # Build query string
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))

        # EIP-712 typed data
        domain = {
            "name": "AsterSignTransaction",
            "chainId": 1666,
        }
        types = {
            "AsterSignTransaction": [
                {"name": "params", "type": "string"},
            ],
        }
        message = {"params": qs}

        acct = Account.from_key(creds["api_secret"])
        full_message = {
            "types": types,
            "primaryType": "AsterSignTransaction",
            "domain": domain,
            "message": message,
        }
        signed = acct.sign_typed_data(full_message=full_message)
        sig_hex = signed.signature.hex()
        if not sig_hex.startswith("0x"):
            sig_hex = "0x" + sig_hex

        headers = {
            "X-AB-APIKEY": creds["api_key"],
        }

        url = BASE + path
        async with httpx.AsyncClient(timeout=10) as c:
            if method == "GET":
                r = await c.get(f"{url}?{qs}&signature={sig_hex}", headers=headers)
            elif method == "POST":
                r = await c.post(f"{url}?{qs}&signature={sig_hex}", headers=headers)
            elif method == "DELETE":
                r = await c.delete(f"{url}?{qs}&signature={sig_hex}", headers=headers)
            else:
                raise ValueError(method)
            if r.status_code >= 400:
                msg = r.text[:200]
                try:
                    j = r.json()
                    msg = str(j.get("msg", msg))
                except Exception:
                    pass
                raise RuntimeError(f"Aster {r.status_code}: {msg}")
            return r.json()

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        data = await cls._signed(creds, "GET", "/fapi/v2/balance")
        for x in (data if isinstance(data, list) else []):
            if x.get("asset") == "USDT":
                return {"usdt": float(x.get("availableBalance", 0) or 0)}
        return {"usdt": 0.0}

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["can_trade"] = True  # if balance works, API wallet has trade perms
            out["balance_usdt"] = bal.get("usdt", 0)
        except Exception as e:
            out["error"] = f"Aster: {str(e)[:180]}"
        return out

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        sym = cls._symbol(symbol)
        try:
            await cls._signed(creds, "POST", "/fapi/v1/marginType",
                              {"symbol": sym, "marginType": "ISOLATED" if margin_mode == "isolated" else "CROSSED"})
        except RuntimeError as e:
            if "No need" not in str(e) and "-4046" not in str(e):
                raise
        await cls._signed(creds, "POST", "/fapi/v1/leverage",
                          {"symbol": sym, "leverage": str(leverage)})

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float) -> dict:
        sym = cls._symbol(symbol)
        r = await cls._signed(creds, "POST", "/fapi/v1/order", {
            "symbol": sym,
            "side": "BUY" if side == "buy" else "SELL",
            "type": "MARKET",
            "quantity": f"{quantity:.6f}".rstrip("0").rstrip("."),
        })
        return {"order_id": str(r.get("orderId", "")), "avg_price": float(r.get("avgPrice", 0) or 0)}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        p = positions[0]
        close_side = "sell" if p["side"] == "buy" else "buy"
        r = await cls.place_order(creds, symbol, close_side, p["quantity"])
        return {"order_id": r.get("order_id"), "closed_qty": p["quantity"], "realized_pnl_usd": 0}

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        params = {"symbol": cls._symbol(symbol)} if symbol else {}
        data = await cls._signed(creds, "GET", "/fapi/v2/positionRisk", params or None)
        out = []
        for p in (data if isinstance(data, list) else []):
            amt = float(p.get("positionAmt", 0) or 0)
            if amt == 0:
                continue
            out.append({
                "exchange": "aster",
                "symbol": str(p.get("symbol", "")).replace("USDT", ""),
                "side": "buy" if amt > 0 else "sell",
                "quantity": abs(amt),
                "entry_price": float(p.get("entryPrice", 0) or 0),
                "mark_price": float(p.get("markPrice", 0) or 0),
                "unrealized_pnl_usd": float(p.get("unRealizedProfit", 0) or 0),
                "leverage": int(float(p.get("leverage", 1) or 1)),
                "position_id": str(p.get("symbol", "")),
            })
        return out

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        return 100

    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        return {"ok": True, "qty_rounded": quantity}
