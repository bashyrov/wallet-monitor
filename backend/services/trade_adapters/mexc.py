"""MEXC USDT-M Futures trade adapter (contract.mexc.com)."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

import httpx

from backend.providers.exchanges._signing import hex_hmac_sha256

BASE = "https://contract.mexc.com"
logger = logging.getLogger("avalant.trade.mexc")

_INSTR_CACHE: dict[str, tuple[dict, float]] = {}
_INSTR_TTL = 600
_INSTR_LOCK = asyncio.Lock()


async def _instrument_info(symbol: str) -> dict | None:
    now = time.time()
    hit = _INSTR_CACHE.get(symbol)
    if hit and now - hit[1] < _INSTR_TTL:
        return hit[0]
    async with _INSTR_LOCK:
        hit = _INSTR_CACHE.get(symbol)
        if hit and time.time() - hit[1] < _INSTR_TTL:
            return hit[0]
        try:
            async with httpx.AsyncClient(timeout=6) as c:
                r = await c.get(f"{BASE}/api/v1/contract/detail?symbol={symbol}")
                j = r.json()
                data = j.get("data")
                if not data:
                    return None
                info = {
                    "minVol": int(data.get("minVol") or 1),
                    "maxVol": int(data.get("maxVol") or 1000000),
                    "contractSize": float(data.get("contractSize") or 1),
                    "priceUnit": float(data.get("priceUnit") or 0.01),
                    "volUnit": int(data.get("volUnit") or 1),
                    "maxLeverage": int(data.get("maxLeverage") or 100),
                }
                _INSTR_CACHE[symbol] = (info, time.time())
                return info
        except Exception as e:
            logger.debug("MEXC instrument fetch failed %s: %s", symbol, e)
            return None


_MEXC_FRIENDLY = {
    "2027": "API key permissions insufficient.",
    "2028": "Invalid API key.",
    "2029": "Invalid signature.",
    "2030": "Timestamp expired — clock skew.",
    "10001": "Insufficient margin balance.",
    "10004": "Insufficient available balance.",
    "10021": "Order quantity below minimum.",
    "10022": "Leverage not available for this symbol.",
    "10060": "Symbol not listed.",
    "10094": "Invalid symbol or contract.",
}


def _friendly_mexc(code: str | None, msg: str) -> str:
    if code and code in _MEXC_FRIENDLY:
        return _MEXC_FRIENDLY[code]
    return msg or "MEXC rejected the request."


def _split_code(exc: Exception) -> tuple[str | None, str]:
    import re
    m = re.match(r"MEXC (\d+): (.*)", str(exc))
    if m:
        return m.group(1), m.group(2)
    return None, str(exc)


class MexcAdapter:
    @staticmethod
    def _symbol(s: str) -> str:
        return s.upper() + "_USDT"

    @classmethod
    async def _signed(cls, creds: dict, method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
        ts = str(int(time.time() * 1000))
        api_key = creds["api_key"]
        secret = creds["api_secret"]

        if method == "GET" and params:
            param_str = "&".join(f"{k}={params[k]}" for k in sorted(params))
        elif body:
            import json as jsonlib
            param_str = jsonlib.dumps(body, separators=(",", ":"))
        else:
            param_str = ""

        sign_str = api_key + ts + param_str
        signature = hex_hmac_sha256(secret, sign_str)

        headers = {
            "ApiKey": api_key,
            "Request-Time": ts,
            "Signature": signature,
            "Content-Type": "application/json",
        }
        url = BASE + path
        async with httpx.AsyncClient(timeout=10) as c:
            if method == "GET":
                r = await c.get(url, params=params, headers=headers)
            else:
                import json as jsonlib
                r = await c.post(url, content=jsonlib.dumps(body or {}, separators=(",", ":")), headers=headers)

        j = r.json()
        code = j.get("code")
        if code is not None and int(code) != 0:
            raise RuntimeError(f"MEXC {code}: {j.get('msg', r.text)}")
        return j.get("data")

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        data = await cls._signed(creds, "GET", "/api/v1/private/account/assets")
        if isinstance(data, list):
            for a in data:
                if a.get("currency") == "USDT":
                    return {"usdt": float(a.get("availableBalance") or 0)}
        return {"usdt": 0.0}

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        sym = cls._symbol(symbol)
        open_type = 1 if margin_mode == "isolated" else 2
        try:
            await cls._signed(creds, "POST", "/api/v1/private/position/change_leverage", body={
                "symbol": sym,
                "leverage": int(leverage),
                "openType": open_type,
            })
        except RuntimeError as e:
            code, msg = _split_code(e)
            raise RuntimeError(_friendly_mexc(code, msg))

    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        sym = cls._symbol(symbol)
        info = await _instrument_info(sym)
        if not info:
            return {"ok": False, "reason": f"{sym} is not listed on MEXC Futures."}
        min_vol = info.get("minVol", 1)
        vol_unit = info.get("volUnit", 1)
        contract_size = info.get("contractSize", 1)
        qty_contracts = int(quantity / contract_size) if contract_size else int(quantity)
        qty_contracts = (qty_contracts // vol_unit) * vol_unit
        if qty_contracts < min_vol:
            return {"ok": False, "reason": f"Quantity below minimum ({min_vol} contracts, each = {contract_size} {symbol.upper()})."}
        try:
            bal = (await cls.fetch_balance(creds)).get("usdt", 0)
        except RuntimeError as e:
            code, msg = _split_code(e)
            return {"ok": False, "reason": _friendly_mexc(code, msg)}
        # Rough margin estimate
        mark_price = 0
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{BASE}/api/v1/contract/ticker?symbol={sym}")
                mark_price = float((r.json().get("data") or {}).get("lastPrice") or 0)
        except Exception:
            pass
        if mark_price and leverage > 0:
            notional = qty_contracts * contract_size * mark_price
            required = notional / max(1, leverage)
            if bal + 0.01 < required:
                return {"ok": False, "reason": f"Insufficient margin: need ~${required:.2f} USDT, have ${bal:.2f}."}
        return {"ok": True, "qty_contracts": qty_contracts, "contract_size": contract_size, "min_vol": min_vol}

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float) -> dict:
        sym = cls._symbol(symbol)
        info = await _instrument_info(sym) or {}
        contract_size = info.get("contractSize", 1)
        vol_unit = info.get("volUnit", 1)
        qty_contracts = int(quantity / contract_size) if contract_size else int(quantity)
        qty_contracts = (qty_contracts // vol_unit) * vol_unit
        if qty_contracts <= 0:
            raise RuntimeError(f"Quantity below minimum for {sym}")
        # side: 1=open_long, 3=open_short
        mexc_side = 1 if side == "buy" else 3
        try:
            data = await cls._signed(creds, "POST", "/api/v1/private/order/submit", body={
                "symbol": sym,
                "price": "0",
                "vol": qty_contracts,
                "side": mexc_side,
                "type": 5,  # market
                "openType": 1,  # isolated default
            })
        except RuntimeError as e:
            code, msg = _split_code(e)
            raise RuntimeError(_friendly_mexc(code, msg))
        order_id = str(data) if isinstance(data, str) else str((data or {}).get("orderId", ""))
        return {"order_id": order_id, "avg_price": 0.0}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        sym = cls._symbol(symbol)
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        p = positions[0]
        # side: 2=close_short (close a long), 4=close_long (close a short)
        mexc_side = 4 if p["side"] == "sell" else 2
        info = await _instrument_info(sym) or {}
        contract_size = info.get("contractSize", 1)
        vol = int(p["quantity"] / contract_size) if contract_size else int(p["quantity"])
        if vol <= 0:
            vol = 1
        try:
            data = await cls._signed(creds, "POST", "/api/v1/private/order/submit", body={
                "symbol": sym,
                "price": "0",
                "vol": vol,
                "side": mexc_side,
                "type": 5,
                "openType": 1,
            })
        except RuntimeError as e:
            code, msg = _split_code(e)
            raise RuntimeError(_friendly_mexc(code, msg))
        order_id = str(data) if isinstance(data, str) else str((data or {}).get("orderId", ""))
        return {"order_id": order_id, "closed_qty": p["quantity"], "realized_pnl_usd": p.get("unrealized_pnl_usd", 0)}

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        params = {}
        if symbol:
            params["symbol"] = cls._symbol(symbol)
        data = await cls._signed(creds, "GET", "/api/v1/private/position/open_positions", params or None)
        out = []
        for p in (data or []):
            vol = float(p.get("holdVol") or 0)
            if vol == 0:
                continue
            pos_type = int(p.get("positionType", 0))  # 1=long, 2=short
            out.append({
                "exchange": "mexc",
                "symbol": str(p.get("symbol", "")).replace("_USDT", ""),
                "side": "buy" if pos_type == 1 else "sell",
                "quantity": vol * float(p.get("contractSize") or 1),
                "entry_price": float(p.get("openAvgPrice") or 0),
                "mark_price": float(p.get("markPrice") or 0),
                "unrealized_pnl_usd": float(p.get("unrealisedPnl") or 0),
                "leverage": int(float(p.get("leverage") or 1)),
                "position_id": str(p.get("positionId", "")),
            })
        return out

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["balance_usdt"] = float(bal.get("usdt") or 0)
        except Exception as e:
            msg = str(e)
            if "2028" in msg:
                out["error"] = "Invalid API key"
            elif "2029" in msg:
                out["error"] = "Signature mismatch — API secret is wrong"
            elif "2027" in msg:
                out["error"] = "Key permissions insufficient"
            else:
                out["error"] = f"MEXC rejected the key: {msg[:180]}"
            return out
        if need_trade:
            out["can_trade"] = True  # MEXC has no separate trade-perm probe; if balance works, key is valid
        return out

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        info = await _instrument_info(cls._symbol(symbol))
        if info:
            return info.get("maxLeverage", 100)
        return 100
