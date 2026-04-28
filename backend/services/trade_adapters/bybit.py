"""Bybit v5 USDT perpetual trade adapter."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json as jsonlib
import logging
import math
import time
from typing import Any

import httpx

BASE = "https://api.bybit.com"
logger = logging.getLogger("avalant.trade.bybit")

_INSTR_CACHE: dict[str, tuple[dict, float]] = {}  # sym → (info, ts)
_INSTR_TTL = 600
_INSTR_LOCK = asyncio.Lock()


async def _instrument_info(symbol: str) -> dict | None:
    """Public instruments-info → qtyStep, minOrderQty, tickSize."""
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
                r = await c.get(f"{BASE}/v5/market/instruments-info?category=linear&symbol={symbol}")
                items = (r.json().get("result") or {}).get("list") or []
                if not items:
                    return None
                it = items[0]
                info = {
                    "qtyStep":     float(it.get("lotSizeFilter", {}).get("qtyStep") or 0),
                    "minOrderQty": float(it.get("lotSizeFilter", {}).get("minOrderQty") or 0),
                    "minNotional": float(it.get("lotSizeFilter", {}).get("minNotionalValue") or 0),
                    "tickSize":    float(it.get("priceFilter",   {}).get("tickSize") or 0),
                    "status":      str(it.get("status") or ""),
                }
                _INSTR_CACHE[symbol] = (info, time.time())
                return info
        except Exception as e:
            logger.debug("instruments-info fetch failed %s: %s", symbol, e)
            return None


_BYBIT_FRIENDLY = {
    "10001": "Bad request to Bybit.",
    "10002": "Request timeout or bad signature.",
    "10003": "Invalid API key.",
    "10004": "Invalid signature.",
    "10005": "API key permissions insufficient.",
    "10006": "Rate limit exceeded — try again in a moment.",
    "10010": "IP not allowed — add the server IP to your key's whitelist.",
    "110004": "Insufficient balance for margin.",
    "110007": "Insufficient available balance.",
    "110012": "Order quantity exceeds position limit.",
    "110017": "Order qty below minimum.",
    "110020": "Order qty not a multiple of lot step.",
    "110025": "Position side not matched (hedge mode).",
    "110043": "Leverage not modified.",
    "110093": "Symbol is not trading right now.",
}


def _friendly_bybit(code: str | None, msg: str) -> str:
    if code and code in _BYBIT_FRIENDLY:
        return _BYBIT_FRIENDLY[code]
    return msg or "Bybit rejected the request."


def _split_code(s: str) -> tuple[str | None, str]:
    import re
    m = re.match(r"Bybit (\d+): (.*)", s)
    if m:
        return m.group(1), m.group(2)
    return None, s


def _round_qty_to_step(qty: float, step: float, min_qty: float) -> float:
    if step > 0:
        qty = math.floor(qty / step) * step
    if min_qty and qty < min_qty:
        return 0.0
    return qty


def _qty_str(q: float) -> str:
    s = f"{q:.8f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".") or "0"
    return s


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
        # Bybit V5 has TWO margin-mode endpoints:
        #   /v5/position/switch-isolated  — per-symbol, works on Classic
        #     and UTA-Inverse. UTA-Linear returns 100028 ("unified account
        #     is forbidden").
        #   /v5/account/set-margin-mode   — account-wide, used by UTA.
        #     Values: REGULAR_MARGIN (cross) | ISOLATED_MARGIN | PORTFOLIO_MARGIN.
        # We try the per-symbol path first (cheaper, doesn't affect other
        # symbols), then fall back to the account-wide one for UTA users.
        async def _mode():
            try:
                await cls._signed(creds, "POST", "/v5/position/switch-isolated", {
                    "category": "linear",
                    "symbol": sym,
                    "tradeMode": 1 if margin_mode == "isolated" else 0,
                    "buyLeverage": str(int(leverage)),
                    "sellLeverage": str(int(leverage)),
                })
            except RuntimeError as e:
                msg = str(e)
                if any(code in msg for code in ("110026", "110043", "110027")):
                    return  # already set / not modified / not allowed-but-OK
                if "100028" in msg:
                    # UTA-Linear: switch via account-level set-margin-mode.
                    setting = "ISOLATED_MARGIN" if margin_mode == "isolated" else "REGULAR_MARGIN"
                    try:
                        await cls._signed(creds, "POST", "/v5/account/set-margin-mode", {
                            "setMarginMode": setting,
                        })
                    except RuntimeError as e2:
                        msg2 = str(e2)
                        # ret_code 30086 = "already in this margin mode" — non-fatal
                        if "30086" in msg2 or "already" in msg2.lower():
                            return
                        import logging as _l
                        _l.getLogger("avalant.trade").warning(
                            "Bybit set-margin-mode failed for %s (%s): %s",
                            sym, setting, msg2,
                        )
                        return
                    return
                raise

        async def _lev():
            try:
                await cls._signed(creds, "POST", "/v5/position/set-leverage", {
                    "category": "linear",
                    "symbol": sym,
                    "buyLeverage": str(int(leverage)),
                    "sellLeverage": str(int(leverage)),
                })
            except RuntimeError as e:
                # 110043 leverage not modified (already set) — non-fatal
                if "110043" not in str(e):
                    raise

        await asyncio.gather(_mode(), _lev())

    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        sym = cls._symbol(symbol)
        info = await _instrument_info(sym)
        if not info:
            return {"ok": False, "reason": f"{sym} is not listed on Bybit."}
        if info.get("status") and info["status"].lower() != "trading":
            return {"ok": False, "reason": f"{sym} is not trading ({info['status']})."}
        step = float(info.get("qtyStep") or 0)
        min_qty = float(info.get("minOrderQty") or 0)
        qty_r = _round_qty_to_step(quantity, step, min_qty)
        if qty_r <= 0:
            return {"ok": False, "reason": f"Quantity below minimum ({min_qty} {symbol.upper()})."}
        # Mark price for notional estimate
        mark_price = 0
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{BASE}/v5/market/tickers?category=linear&symbol={sym}")
                items = (r.json().get("result") or {}).get("list") or []
                if items:
                    mark_price = float(items[0].get("markPrice") or items[0].get("lastPrice") or 0)
        except Exception:
            pass
        min_notional = float(info.get("minNotional") or 0)
        if mark_price and min_notional and qty_r * mark_price < min_notional:
            return {"ok": False, "reason": f"Notional below minimum (~${qty_r * mark_price:.2f} < ${min_notional:.2f})."}
        try:
            bal = (await cls.fetch_balance(creds)).get("usdt", 0)
        except RuntimeError as e:
            code, msg = _split_code(str(e))
            return {"ok": False, "reason": _friendly_bybit(code, msg)}
        if mark_price and leverage > 0:
            required = (qty_r * mark_price) / max(1, leverage)
            if bal + 0.01 < required:
                return {"ok": False, "reason": f"Insufficient margin: need ~${required:.2f} USDT, have ${bal:.2f}."}
        return {"ok": True, "qty_rounded": qty_r, "step_size": step, "min_qty": min_qty, "min_notional": min_notional}

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        sym = cls._symbol(symbol)
        info = await _instrument_info(sym) or {}
        step = float(info.get("qtyStep") or 0)
        min_qty = float(info.get("minOrderQty") or 0)
        qty_r = _round_qty_to_step(quantity, step, min_qty)
        if qty_r <= 0:
            raise RuntimeError(f"Quantity below minimum ({min_qty} {symbol.upper()})")
        try:
            r = await cls._signed(creds, "POST", "/v5/order/create", {
                "category": "linear",
                "symbol": sym,
                "side": "Buy" if side == "buy" else "Sell",
                "orderType": "Market",
                "qty": _qty_str(qty_r),
            })
        except RuntimeError as e:
            code, msg = _split_code(str(e))
            raise RuntimeError(_friendly_bybit(code, msg))
        return {"order_id": str(r.get("orderId", "")), "avg_price": 0.0}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        sym = cls._symbol(symbol)
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        p = positions[0]
        reduce_side = "Sell" if p["side"] == "buy" else "Buy"
        try:
            r = await cls._signed(creds, "POST", "/v5/order/create", {
                "category": "linear",
                "symbol": sym,
                "side": reduce_side,
                "orderType": "Market",
                "qty": _qty_str(p['quantity']),
                "reduceOnly": True,
            })
        except RuntimeError as e:
            code, msg = _split_code(str(e))
            raise RuntimeError(_friendly_bybit(code, msg))
        return {"order_id": str(r.get("orderId", "")), "closed_qty": p["quantity"], "realized_pnl_usd": p.get("unrealized_pnl_usd", 0)}

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        """Return {can_read, can_trade, balance_usdt, error}. Never raises."""
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        # 1) balance = read test
        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["balance_usdt"] = float(bal.get("usdt") or 0)
        except Exception as e:
            msg = str(e)
            if "10003" in msg or "10004" in msg or "API key is invalid" in msg:
                out["error"] = "Invalid API key"
            elif "10005" in msg or "permission" in msg.lower():
                out["error"] = "Key permissions insufficient"
            elif "10002" in msg or "sign" in msg.lower():
                out["error"] = "Signature mismatch — API secret is wrong"
            else:
                out["error"] = f"Bybit rejected the key: {msg[:180]}"
            return out
        # 2) trade permissions via /v5/user/query-api
        if need_trade:
            try:
                info = await cls._signed(creds, "GET", "/v5/user/query-api", {})
                perms = info.get("permissions") or {}
                contract = perms.get("ContractTrade") or []
                # A trade-enabled key has "Order" or "Position" inside ContractTrade
                if any(p in ("Order", "Position") for p in contract):
                    out["can_trade"] = True
                else:
                    out["error"] = "Key has no Contract Trade permission (enable Order/Position on Bybit)"
            except Exception as e:
                out["error"] = f"Trade-permission probe failed: {str(e)[:180]}"
        return out

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
    async def _funding_pnl(cls, creds: dict, api_symbol: str, since_ms: int) -> float | None:
        """Sum funding settlements for `api_symbol` via transaction log.
        `/v5/account/transaction-log?category=linear&type=SETTLEMENT` — SETTLEMENT
        covers periodic funding fees; `change` field is the USDT delta (negative
        = paid)."""
        try:
            data = await cls._signed(creds, "GET", "/v5/account/transaction-log", {
                "category": "linear", "type": "SETTLEMENT",
                "symbol": api_symbol, "startTime": since_ms, "limit": 50,
            })
            rows = (data or {}).get("list") or []
            return sum(float(x.get("change") or 0) for x in rows)
        except Exception:
            return None

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        params = {"category": "linear"}
        if symbol:
            params["symbol"] = cls._symbol(symbol)
        else:
            params["settleCoin"] = "USDT"
        data = await cls._signed(creds, "GET", "/v5/position/list", params)
        positions = []
        for p in data.get("list", []):
            qty = float(p.get("size") or 0)
            if qty == 0:
                continue
            side = "buy" if p.get("side") == "Buy" else "sell"
            # Bybit margin-mode determination:
            #   Classic: position.tradeMode (0=cross, 1=isolated) is reliable
            #   UTA-Linear: tradeMode is always 0 regardless of actual mode;
            #     truth is at account level (account.marginMode).
            # If tradeMode is 1 we know it's isolated. If 0 we need to
            # fall back to account info for UTA users.
            tm = p.get("tradeMode")
            if tm == 1:
                margin_mode = "isolated"
            elif tm == 0:
                # Defer to account-level mode (one extra REST call, cached
                # below for the rest of this list_positions invocation).
                margin_mode = "_uta_lookup"
            else:
                margin_mode = None
            positions.append({
                "exchange": "bybit",
                "symbol": str(p.get("symbol", "")).replace("USDT", ""),
                "_api_symbol": str(p.get("symbol", "")),
                "side": side,
                "quantity": qty,
                "entry_price": float(p.get("avgPrice") or 0),
                "mark_price":  float(p.get("markPrice") or 0),
                "unrealized_pnl_usd": float(p.get("unrealisedPnl") or 0),
                "leverage": int(float(p.get("leverage") or 1)),
                "margin_mode": margin_mode,
                "position_id": str(p.get("symbol", "")),
            })
        if not positions:
            return []
        # UTA users: fetch account.marginMode once and patch any position
        # that we couldn't resolve from tradeMode alone.
        needs_uta = any(p.get("margin_mode") == "_uta_lookup" for p in positions)
        if needs_uta:
            try:
                info = await cls._signed(creds, "GET", "/v5/account/info", {})
                acct_mode = (info.get("marginMode") or "").upper()
                if acct_mode.startswith("ISOLATED"):
                    uta_mode = "isolated"
                elif acct_mode.startswith("REGULAR") or acct_mode.startswith("PORTFOLIO"):
                    uta_mode = "cross"
                else:
                    uta_mode = None
            except Exception:
                uta_mode = None
            for p in positions:
                if p.get("margin_mode") == "_uta_lookup":
                    p["margin_mode"] = uta_mode
        since_ms = int((time.time() - 7 * 86400) * 1000)
        fundings = await asyncio.gather(*[
            cls._funding_pnl(creds, p["_api_symbol"], since_ms) for p in positions
        ], return_exceptions=True)
        for p, f in zip(positions, fundings):
            p["funding_pnl_usd"] = f if isinstance(f, (int, float)) else None
            p.pop("_api_symbol", None)
        return positions
