"""WhiteBIT Collateral Futures trade adapter."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import time
from typing import Any

import httpx

BASE = "https://whitebit.com"
logger = logging.getLogger("avalant.trade.whitebit")

# ── Instrument cache ──
_INSTR_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_INSTR_TTL = 600
_INSTR_LOCK = asyncio.Lock()


async def _instruments() -> dict[str, dict]:
    """Return {symbol: {min_amount, tick_size, ...}} from public futures endpoint."""
    now = time.time()
    if _INSTR_CACHE["data"] and now - _INSTR_CACHE["ts"] < _INSTR_TTL:
        return _INSTR_CACHE["data"]
    async with _INSTR_LOCK:
        if _INSTR_CACHE["data"] and time.time() - _INSTR_CACHE["ts"] < _INSTR_TTL:
            return _INSTR_CACHE["data"]
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{BASE}/api/v4/public/futures")
                body = r.json()
        except Exception as e:
            logger.warning("WhiteBIT futures instruments failed: %s", e)
            return _INSTR_CACHE["data"] or {}
        out: dict[str, dict] = {}
        items = body if isinstance(body, list) else body.get("result", [])
        for s in items:
            name = s.get("ticker_id") or s.get("name") or ""
            if not name:
                continue
            out[name] = {
                "min_amount": float(s.get("min_amount") or s.get("minAmount") or 0),
                "tick_size": float(s.get("tick_size") or s.get("tickSize") or 0),
                "stock_prec": int(s.get("stock_prec") or s.get("stockPrec") or 4),
            }
        _INSTR_CACHE["data"] = out
        _INSTR_CACHE["ts"] = time.time()
        return out


_FRIENDLY = {
    "Balance not enough": "Insufficient margin.",
    "Invalid payload": "Signature/payload mismatch — check API secret.",
    "This action is unauthorized": "API key has no trade permission.",
}


def _friendly_error(msg: str) -> str:
    for key, friendly in _FRIENDLY.items():
        if key in msg:
            return friendly
    return msg or "WhiteBIT rejected the request."


def _round_qty(qty: float, prec: int) -> float:
    factor = 10 ** prec
    return math.floor(qty * factor) / factor


def _qty_str(qty: float, prec: int) -> str:
    s = f"{qty:.{max(prec, 0)}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".") or "0"
    return s


class WhitebitAdapter:
    @staticmethod
    def _sign(body_json: str, secret: str) -> tuple[str, str]:
        """Returns (payload_b64, signature)."""
        payload_b64 = base64.b64encode(body_json.encode()).decode()
        sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha512).hexdigest()
        return payload_b64, sig

    @classmethod
    async def _req(cls, creds: dict, path: str, body: dict | None = None) -> Any:
        body = dict(body or {})
        body["request"] = path
        body["nonce"] = int(time.time() * 1000)
        body_json = json.dumps(body, separators=(",", ":"))
        payload_b64, sig = cls._sign(body_json, creds["api_secret"])
        headers = {
            "X-TXC-APIKEY": creds["api_key"],
            "X-TXC-PAYLOAD": payload_b64,
            "X-TXC-SIGNATURE": sig,
            "Content-Type": "application/json",
        }
        url = BASE + path
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, content=body_json, headers=headers)
            if r.status_code >= 400:
                msg = r.text
                try:
                    j = r.json()
                    msg = str(j.get("message") or j.get("errors") or r.text)
                except Exception:
                    pass
                raise RuntimeError(f"WhiteBIT {r.status_code}: {msg}")
            data = r.json()
            if isinstance(data, dict) and data.get("code") and data.get("code") != 0:
                raise RuntimeError(f"WhiteBIT: {data.get('message', data)}")
            return data

    @staticmethod
    def _symbol(s: str) -> str:
        return s.upper() + "_PERP"

    # ── Balance ──
    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        data = await cls._req(creds, "/api/v4/trade-account/balance")
        usdt = 0.0
        if isinstance(data, dict):
            entry = data.get("USDT", {})
            usdt = float(entry.get("available") or entry.get("balance") or 0)
        return {"usdt": usdt}

    # ── Leverage ──
    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        # WhiteBIT doesn't have per-symbol leverage via standard API — stub
        pass

    @classmethod
    async def get_public_qty_limits(cls, symbol: str) -> dict | None:
        info = (await _instruments()).get(cls._symbol(symbol))
        if not info:
            return None
        prec = int(info.get("stock_prec") or 4)
        return {
            "min_qty": float(info.get("min_amount") or 0),
            "step":    10 ** (-prec) if prec > 0 else None,
            "max_qty": None,
            "unit": "coin",
        }

    # ── Preflight ──
    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        sym = cls._symbol(symbol)
        info = (await _instruments()).get(sym)
        if not info:
            return {"ok": False, "reason": f"Symbol {sym} not listed on WhiteBIT Futures."}
        prec = info.get("stock_prec", 4)
        min_amt = info.get("min_amount", 0)
        qty_r = _round_qty(quantity, prec)
        if qty_r <= 0 or qty_r < min_amt:
            return {"ok": False, "reason": f"Quantity below minimum ({min_amt} {symbol.upper()})."}
        try:
            bal = (await cls.fetch_balance(creds)).get("usdt", 0)
        except RuntimeError as e:
            return {"ok": False, "reason": _friendly_error(str(e))}
        return {"ok": True, "qty_rounded": qty_r, "precision": prec, "min_qty": min_amt}

    # ── Place order ──
    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        sym = cls._symbol(symbol)
        info = (await _instruments()).get(sym) or {}
        prec = info.get("stock_prec", 4)
        qty_r = _round_qty(quantity, prec)
        qty_s = _qty_str(qty_r, prec)
        try:
            r = await cls._req(creds, "/api/v4/order/collateral/market", {
                "market": sym,
                "side": "buy" if side == "buy" else "sell",
                "amount": qty_s,
            })
        except RuntimeError as e:
            raise RuntimeError(_friendly_error(str(e)))
        return {"order_id": str(r.get("orderId") or r.get("id", "")), "avg_price": float(r.get("dealMoney", 0) or 0)}

    # ── Close position ──
    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        sym = cls._symbol(symbol)
        positions = await cls.list_positions(creds, symbol)
        target = next((p for p in positions if p["quantity"] != 0), None)
        if not target:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        amt = target["quantity"]
        reduce_side = "sell" if target["side"] == "buy" else "buy"
        info = (await _instruments()).get(sym) or {}
        prec = info.get("stock_prec", 4)
        qty_s = _qty_str(amt, prec)
        try:
            r = await cls._req(creds, "/api/v4/order/collateral/market", {
                "market": sym,
                "side": reduce_side,
                "amount": qty_s,
                "reduceOnly": True,
            })
        except RuntimeError as e:
            raise RuntimeError(_friendly_error(str(e)))
        return {"order_id": str(r.get("orderId") or r.get("id", "")), "closed_qty": amt, "realized_pnl_usd": 0.0}

    # ── Positions ──
    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        data = await cls._req(creds, "/api/v4/collateral-account/positions/open")
        pos_list = data if isinstance(data, list) else []
        out = []
        for p in pos_list:
            amt = float(p.get("amount") or p.get("baseAmount") or 0)
            if amt == 0:
                continue
            market = str(p.get("market") or "")
            sym_clean = market.replace("_PERP", "")
            if symbol and sym_clean.upper() != symbol.upper():
                continue
            try:
                funding = float(p.get("fundingFee") or p.get("funding") or 0)
            except (TypeError, ValueError):
                funding = 0.0
            out.append({
                "exchange": "whitebit",
                "symbol": sym_clean,
                "side": "buy" if amt > 0 else "sell",
                "quantity": abs(amt),
                "entry_price": float(p.get("entryPrice") or p.get("basePrice") or 0),
                "mark_price": float(p.get("markPrice") or p.get("currentPrice") or 0),
                "unrealized_pnl_usd": float(p.get("unrealizedPnl") or p.get("pnl") or 0),
                "funding_pnl_usd": funding if funding else None,
                "leverage": int(float(p.get("leverage") or 1)),
                "position_id": market,
            })
        return out

    # ── Validate key ──
    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["balance_usdt"] = float(bal.get("usdt") or 0)
        except Exception as e:
            msg = str(e)
            if "unauthorized" in msg.lower():
                out["error"] = "API key rejected by WhiteBIT"
            elif "Invalid payload" in msg:
                out["error"] = "Signature mismatch — API secret is wrong"
            else:
                out["error"] = f"WhiteBIT rejected the key: {msg[:180]}"
            return out
        if need_trade:
            out["can_trade"] = True
        return out

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        return 100
