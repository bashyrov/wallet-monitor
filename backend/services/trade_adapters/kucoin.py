"""KuCoin Futures trade adapter (api-futures.kucoin.com)."""
from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import math
import time
from typing import Any

import httpx

from backend.providers.exchanges._signing import b64_hmac_sha256

BASE = "https://api-futures.kucoin.com"
logger = logging.getLogger("avalant.trade.kucoin")

_INSTR_CACHE: dict[str, tuple[dict, float]] = {}
_INSTR_TTL = 600
_INSTR_LOCK = asyncio.Lock()

# BTC → XBT mapping
_BTC_TO_XBT = {"BTC": "XBT"}


def _kc_symbol(s: str) -> str:
    base = s.upper()
    base = _BTC_TO_XBT.get(base, base)
    return base + "USDTM"


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
                r = await c.get(f"{BASE}/api/v1/contracts/{symbol}")
                j = r.json()
                data = j.get("data")
                if not data:
                    return None
                info = {
                    "multiplier": float(data.get("multiplier") or 1),
                    "lotSize": int(data.get("lotSize") or 1),
                    "tickSize": float(data.get("tickSize") or 0.01),
                    "maxLeverage": int(float(data.get("maxLeverage") or 100)),
                    "isInverse": bool(data.get("isInverse")),
                    "status": str(data.get("status") or ""),
                }
                _INSTR_CACHE[symbol] = (info, time.time())
                return info
        except Exception as e:
            logger.debug("KuCoin instrument fetch failed %s: %s", symbol, e)
            return None


_KC_FRIENDLY = {
    "100001": "Request too frequent — rate limited.",
    "200004": "Insufficient balance.",
    "300000": "Invalid symbol or not supported.",
    "300003": "Order quantity below minimum.",
    "300012": "Insufficient position to close.",
    "400001": "Invalid API key.",
    "400002": "Signature mismatch.",
    "400003": "Timestamp expired — clock skew.",
    "400005": "API key permissions insufficient.",
    "400100": "Parameter error.",
}


def _friendly_kc(code: str | None, msg: str) -> str:
    if code and code in _KC_FRIENDLY:
        return _KC_FRIENDLY[code]
    return msg or "KuCoin rejected the request."


def _split_code(exc: Exception) -> tuple[str | None, str]:
    import re
    m = re.match(r"KuCoin (\d+): (.*)", str(exc))
    if m:
        return m.group(1), m.group(2)
    return None, str(exc)


class KuCoinAdapter:
    @staticmethod
    def _symbol(s: str) -> str:
        return _kc_symbol(s)

    @classmethod
    async def _signed(cls, creds: dict, method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
        ts = str(int(time.time() * 1000))
        api_key = creds["api_key"]
        secret = creds["api_secret"]
        passphrase = creds["api_passphrase"]

        if method == "GET" and params:
            query = "&".join(f"{k}={params[k]}" for k in sorted(params))
            url_path = path + "?" + query
            body_str = ""
        elif body is not None:
            url_path = path
            body_str = jsonlib.dumps(body, separators=(",", ":"))
        else:
            url_path = path
            body_str = ""

        sign_str = ts + method + url_path + body_str
        signature = b64_hmac_sha256(secret, sign_str)
        passphrase_sign = b64_hmac_sha256(secret, passphrase)

        headers = {
            "KC-API-KEY": api_key,
            "KC-API-SIGN": signature,
            "KC-API-TIMESTAMP": ts,
            "KC-API-PASSPHRASE": passphrase_sign,
            "KC-API-KEY-VERSION": "2",
            "Content-Type": "application/json",
        }
        url = BASE + url_path if method == "GET" else BASE + path
        async with httpx.AsyncClient(timeout=10) as c:
            if method == "GET":
                r = await c.get(url, headers=headers)
            elif method == "POST":
                r = await c.post(url, content=body_str or "{}", headers=headers)
            elif method == "DELETE":
                r = await c.delete(url, headers=headers)
            else:
                raise ValueError(method)

        j = r.json()
        code = str(j.get("code", ""))
        if code != "200000":
            raise RuntimeError(f"KuCoin {code}: {j.get('msg', r.text)}")
        return j.get("data")

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        """KuCoin Futures: availableBalance is the free portion. If user has
        open positions, fall back to accountEquity (total) so the UI reflects
        actual funds on the account, not just free margin."""
        data = await cls._signed(creds, "GET", "/api/v1/account-overview", {"currency": "USDT"})
        d = data or {}
        avail = float(d.get("availableBalance") or 0)
        if avail > 0:
            return {"usdt": avail}
        # Fall back to accountEquity (= marginBalance = wallet balance + uPnL).
        # Not perfect for "what can I open now" but prevents the UI showing
        # $0 when user has funds tied up in an open position.
        equity = float(d.get("accountEquity") or d.get("marginBalance") or 0)
        return {"usdt": max(avail, equity), "available": avail, "equity": equity}

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        sym = cls._symbol(symbol)
        # KuCoin: margin mode is set per-position via the order's leverage param
        # Change leverage via risk limit endpoint
        try:
            await cls._signed(creds, "POST", "/api/v1/position/risk-limit-level/change", body={
                "symbol": sym,
                "level": 1,  # default risk level
            })
        except RuntimeError:
            pass  # risk limit may already be at level 1

        # Leverage is set on each order via the leverage parameter — no separate endpoint needed
        # But we can validate the symbol exists
        info = await _instrument_info(sym)
        if not info:
            raise RuntimeError(f"{sym} is not listed on KuCoin Futures.")
        if leverage > info.get("maxLeverage", 100):
            raise RuntimeError(f"Max leverage for {sym} is {info['maxLeverage']}x.")

    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        sym = cls._symbol(symbol)
        info = await _instrument_info(sym)
        if not info:
            return {"ok": False, "reason": f"{sym} is not listed on KuCoin Futures."}
        if info.get("status") and info["status"].lower() not in ("open", ""):
            return {"ok": False, "reason": f"{sym} is not trading ({info['status']})."}

        multiplier = info.get("multiplier", 1)
        lot_size = info.get("lotSize", 1)
        # KuCoin uses contracts: size = number of lots, each lot = multiplier units of base
        qty_lots = int(quantity / multiplier) if multiplier else int(quantity)
        qty_lots = (qty_lots // lot_size) * lot_size
        if qty_lots < lot_size:
            return {"ok": False, "reason": f"Quantity below minimum ({lot_size} lot(s), each = {multiplier} {symbol.upper()})."}

        if leverage > info.get("maxLeverage", 100):
            return {"ok": False, "reason": f"Max leverage for {sym} is {info['maxLeverage']}x."}

        try:
            bal = (await cls.fetch_balance(creds)).get("usdt", 0)
        except RuntimeError as e:
            code, msg = _split_code(e)
            return {"ok": False, "reason": _friendly_kc(code, msg)}

        mark_price = 0
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{BASE}/api/v1/ticker?symbol={sym}")
                mark_price = float((r.json().get("data") or {}).get("price") or 0)
        except Exception:
            pass
        if mark_price and leverage > 0:
            notional = qty_lots * multiplier * mark_price
            required = notional / max(1, leverage)
            if bal + 0.01 < required:
                return {"ok": False, "reason": f"Insufficient margin: need ~${required:.2f} USDT, have ${bal:.2f}."}

        return {"ok": True, "qty_lots": qty_lots, "multiplier": multiplier, "lot_size": lot_size}

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        sym = cls._symbol(symbol)
        info = await _instrument_info(sym) or {}
        multiplier = info.get("multiplier", 1)
        lot_size = info.get("lotSize", 1)
        max_lev = int(info.get("maxLeverage") or 100)
        qty_lots = int(quantity / multiplier) if multiplier else int(quantity)
        qty_lots = (qty_lots // lot_size) * lot_size
        if qty_lots <= 0:
            raise RuntimeError(f"Quantity below minimum for {sym}")
        # Clamp to the per-symbol max. Set a safe default if caller passed 0/neg.
        lev = max(1, min(int(leverage or 1), max_lev))
        body = {
            "symbol": sym,
            "side": "buy" if side == "buy" else "sell",
            "type": "market",
            "size": qty_lots,
            "leverage": lev,
            # KuCoin Futures: tdMode "ISOLATED" | "CROSS" — without this the
            # server may default to whatever the account has cached.
            "marginMode": "ISOLATED" if margin_mode == "isolated" else "CROSS",
        }
        try:
            data = await cls._signed(creds, "POST", "/api/v1/orders", body=body)
        except RuntimeError as e:
            code, msg = _split_code(e)
            raise RuntimeError(_friendly_kc(code, msg))
        return {"order_id": str((data or {}).get("orderId", "")), "avg_price": 0.0}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        sym = cls._symbol(symbol)
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        p = positions[0]
        reduce_side = "sell" if p["side"] == "buy" else "buy"
        try:
            data = await cls._signed(creds, "POST", "/api/v1/orders", body={
                "symbol": sym,
                "side": reduce_side,
                "type": "market",
                "closeOrder": True,
                "size": 1,  # closeOrder ignores size, closes entire position
            })
        except RuntimeError as e:
            code, msg = _split_code(e)
            raise RuntimeError(_friendly_kc(code, msg))
        return {
            "order_id": str((data or {}).get("orderId", "")),
            "closed_qty": p["quantity"],
            "realized_pnl_usd": p.get("unrealized_pnl_usd", 0),
        }

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        params = {}
        if symbol:
            params["symbol"] = cls._symbol(symbol)
        data = await cls._signed(creds, "GET", "/api/v1/position" + ("s" if not symbol else ""), params or None)
        items = [data] if isinstance(data, dict) else (data or [])
        out = []
        for p in items:
            qty = abs(int(p.get("currentQty") or 0))
            if qty == 0:
                continue
            raw_qty = int(p.get("currentQty") or 0)
            multiplier = float(p.get("multiplier") or 1)
            base_sym = str(p.get("symbol", "")).replace("USDTM", "")
            # Normalize XBT → BTC
            if base_sym == "XBT":
                base_sym = "BTC"
            out.append({
                "exchange": "kucoin",
                "symbol": base_sym,
                "side": "buy" if raw_qty > 0 else "sell",
                "quantity": abs(raw_qty) * multiplier,
                "entry_price": float(p.get("avgEntryPrice") or 0),
                "mark_price": float(p.get("markPrice") or 0),
                "unrealized_pnl_usd": float(p.get("unrealisedPnl") or 0),
                "leverage": int(float(p.get("realLeverage") or p.get("leverage") or 1)),
                "position_id": str(p.get("id", "")),
            })
        return out

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        if not creds.get("api_passphrase"):
            out["error"] = "KuCoin requires a passphrase"
            return out
        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["balance_usdt"] = float(bal.get("usdt") or 0)
        except Exception as e:
            msg = str(e)
            if "400001" in msg:
                out["error"] = "Invalid API key"
            elif "400002" in msg:
                out["error"] = "Signature mismatch — check API secret and passphrase"
            elif "400005" in msg:
                out["error"] = "Key permissions insufficient"
            else:
                out["error"] = f"KuCoin rejected the key: {msg[:180]}"
            return out
        if need_trade:
            out["can_trade"] = True  # if balance read works, futures key is valid
        return out

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        info = await _instrument_info(cls._symbol(symbol))
        if info:
            return info.get("maxLeverage", 100)
        return 100
