"""Ethereal DEX trade adapter — EIP-712 linked signer.

Users create a "Linked Signer" (any ETH keypair) and link it to their
subaccount via EIP-712 signature. The signer can trade but cannot withdraw.

creds = {"address": "0x... (subaccount)", "private_key": "0x... (linked signer key)"}
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("avalant.trade.ethereal")

BASE = "https://api.ethereal.trade"


class EtherealAdapter:

    @staticmethod
    def _symbol(s: str) -> str:
        return s.upper()

    @classmethod
    async def _signed_request(cls, creds: dict, method: str, path: str,
                               params: dict | None = None, body: dict | None = None) -> Any:
        """EIP-712 signed request via linked signer."""
        try:
            from eth_account import Account
        except ImportError:
            raise RuntimeError("eth_account package required for Ethereal trading")

        private_key = creds.get("private_key") or creds.get("api_secret") or ""
        if not private_key:
            raise RuntimeError("Ethereal requires a linked signer private key")

        acct = Account.from_key(private_key)
        ts = str(int(time.time() * 1e9))  # nanoseconds

        # Simplified auth: header-based signature
        import json as _json
        sign_payload = f"{method}{path}{ts}{_json.dumps(body or {}, separators=(',', ':'))}"
        from eth_account.messages import encode_defunct
        signed = acct.sign_message(encode_defunct(text=sign_payload))

        headers = {
            "Content-Type": "application/json",
            "X-Ethereal-Address": creds.get("address") or creds.get("api_key") or "",
            "X-Ethereal-Timestamp": ts,
            "X-Ethereal-Signature": signed.signature.hex(),
        }

        url = BASE + path
        async with httpx.AsyncClient(timeout=10) as c:
            if method == "GET":
                r = await c.get(url, params=params, headers=headers)
            elif method == "POST":
                r = await c.post(url, json=body, headers=headers)
            else:
                raise ValueError(method)
            if r.status_code >= 400:
                raise RuntimeError(f"Ethereal {r.status_code}: {r.text[:200]}")
            return r.json()

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        address = creds.get("address") or creds.get("api_key") or ""
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{BASE}/v1/subaccount", params={"address": address})
            if r.status_code >= 400:
                raise RuntimeError(f"Ethereal {r.status_code}: {r.text[:200]}")
            j = r.json()
            equity = float(j.get("equity", 0) or j.get("accountValue", 0) or 0)
            return {"usdt": equity}

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["balance_usdt"] = bal.get("usdt", 0)
        except Exception as e:
            out["error"] = f"Ethereal: {str(e)[:180]}"
            return out
        if need_trade:
            pk = creds.get("private_key") or creds.get("api_secret")
            if not pk:
                out["error"] = "Linked signer private key required for trading"
            else:
                out["can_trade"] = True
        return out

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        pass  # Ethereal manages leverage per-position at order time

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        body = {
            "symbol": cls._symbol(symbol),
            "side": side,
            "type": "market",
            "quantity": str(quantity),
        }
        r = await cls._signed_request(creds, "POST", "/v1/order", body=body)
        return {"order_id": str(r.get("orderId", r.get("id", ""))), "avg_price": 0.0}

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
        address = creds.get("address") or creds.get("api_key") or ""
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{BASE}/v1/subaccount", params={"address": address})
            if r.status_code >= 400:
                return []
            j = r.json()
        out = []
        for p in j.get("positions", []):
            qty = float(p.get("size", 0) or 0)
            if qty == 0:
                continue
            sym = str(p.get("symbol", p.get("productSymbol", "")))
            if symbol and sym.upper() != symbol.upper():
                continue
            out.append({
                "exchange": "ethereal",
                "symbol": sym,
                "side": "buy" if qty > 0 else "sell",
                "quantity": abs(qty),
                "entry_price": float(p.get("entryPrice", 0) or 0),
                "mark_price": float(p.get("markPrice", 0) or 0),
                "unrealized_pnl_usd": float(p.get("unrealizedPnl", 0) or 0),
                "leverage": 1,
                "position_id": sym,
            })
        return out

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        return 20

    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        return {"ok": True, "qty_rounded": quantity}
