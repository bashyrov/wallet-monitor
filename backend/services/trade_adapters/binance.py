"""Binance USDT-M Futures trade adapter (FAPI)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import time
import urllib.parse
from typing import Any

import httpx

BASE = "https://fapi.binance.com"
logger = logging.getLogger("avalant.trade.binance")


# ── Symbol filter cache: public exchangeInfo refreshed lazily every 10 min ──
_EX_INFO_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_EX_INFO_TTL = 600  # seconds
_EX_INFO_LOCK = asyncio.Lock()


async def _exchange_info() -> dict[str, dict]:
    """Return {symbol: {stepSize, minQty, minNotional, tickSize, quantityPrecision, pricePrecision}}."""
    now = time.time()
    if _EX_INFO_CACHE["data"] and now - _EX_INFO_CACHE["ts"] < _EX_INFO_TTL:
        return _EX_INFO_CACHE["data"]
    async with _EX_INFO_LOCK:
        if _EX_INFO_CACHE["data"] and time.time() - _EX_INFO_CACHE["ts"] < _EX_INFO_TTL:
            return _EX_INFO_CACHE["data"]
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{BASE}/fapi/v1/exchangeInfo")
                data = r.json()
        except Exception as e:
            logger.warning("exchangeInfo fetch failed: %s", e)
            return _EX_INFO_CACHE["data"] or {}
        out: dict[str, dict] = {}
        for s in data.get("symbols", []):
            sym = s.get("symbol")
            if not sym or s.get("contractType") != "PERPETUAL":
                continue
            info = {
                "stepSize": None, "minQty": None, "minNotional": None, "tickSize": None,
                "quantityPrecision": int(s.get("quantityPrecision", 8) or 8),
                "pricePrecision":    int(s.get("pricePrecision", 8) or 8),
            }
            for f in s.get("filters", []):
                t = f.get("filterType")
                if t == "LOT_SIZE":
                    info["stepSize"] = float(f.get("stepSize") or 0)
                    info["minQty"]   = float(f.get("minQty")   or 0)
                elif t == "MIN_NOTIONAL":
                    info["minNotional"] = float(f.get("notional") or f.get("minNotional") or 0)
                elif t == "PRICE_FILTER":
                    info["tickSize"] = float(f.get("tickSize") or 0)
            out[sym] = info
        _EX_INFO_CACHE["data"] = out
        _EX_INFO_CACHE["ts"] = time.time()
        return out


# ── Position-mode cache (hedge vs one-way) per API key ──
_MODE_CACHE: dict[str, tuple[bool, float]] = {}  # api_key → (dualSidePosition, ts)
_MODE_TTL = 300


# ── Error code → friendly message ──
_BINANCE_FRIENDLY = {
    "-1013": "Order does not meet the exchange's minimum size/notional.",
    "-1021": "Clock skew — try again in a moment.",
    "-1022": "Signature mismatch — API secret is wrong.",
    "-1111": "Quantity has more decimals than the contract allows.",
    "-1121": "Symbol not listed on Binance Futures.",
    "-2010": "Order rejected by the exchange.",
    "-2014": "Invalid API key.",
    "-2015": "Binance rejected the key (check IP whitelist and permissions).",
    "-2019": "Insufficient margin — your USDT balance is too low for this size/leverage.",
    "-4046": "Margin mode already set.",
    "-4061": "Position side does not match account mode. Your account is in hedge mode.",
    "-4164": "Order size below minimum notional.",
}


def _friendly_error(code: str | None, msg: str) -> str:
    if code and code in _BINANCE_FRIENDLY:
        return _BINANCE_FRIENDLY[code]
    return msg or "Binance rejected the request."


def _round_qty_to_step(qty: float, step: float | None, precision: int) -> float:
    if step and step > 0:
        return math.floor(qty / step) * step
    factor = 10 ** precision
    return math.floor(qty * factor) / factor


def _qty_to_str(qty: float, precision: int) -> str:
    s = f"{qty:.{max(precision, 0)}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".") or "0"
    return s


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
                code = None
                msg = r.text
                try:
                    j = r.json()
                    code = str(j.get("code")) if j.get("code") is not None else None
                    msg  = str(j.get("msg") or r.text)
                except Exception:
                    pass
                raise RuntimeError(f"Binance {r.status_code} {code or ''}: {msg}".strip())
            return r.json()

    @staticmethod
    def _symbol(s: str) -> str:
        return s.upper() + "USDT"

    # ── Balance ──
    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        """Return available Futures USDT. If a user holds funds in cross-wallet
        but has positions open, `availableBalance` can be low or zero — fall
        back to `crossWalletBalance` (total margin wallet) so the UI shows the
        real money, not just the free portion."""
        data = await cls._signed(creds, "GET", "/fapi/v2/balance")
        for x in data:
            if x.get("asset") == "USDT":
                avail = float(x.get("availableBalance", 0) or 0)
                if avail > 0:
                    return {"usdt": avail, "total": float(x.get("balance", 0) or 0)}
                # availableBalance=0 → try crossWalletBalance / balance. Means
                # user has funds but they're currently used as margin.
                total = float(x.get("balance", 0) or 0)
                cross = float(x.get("crossWalletBalance", 0) or 0)
                return {"usdt": max(avail, cross, total), "total": total,
                        "available": avail}
        return {"usdt": 0.0}

    # ── Position mode ──
    @classmethod
    async def _is_hedge_mode(cls, creds: dict) -> bool:
        key = str(creds.get("api_key") or "")
        hit = _MODE_CACHE.get(key)
        if hit and time.time() - hit[1] < _MODE_TTL:
            return hit[0]
        try:
            r = await cls._signed(creds, "GET", "/fapi/v1/positionSide/dual")
            dual = bool(r.get("dualSidePosition"))
        except Exception:
            dual = False
        _MODE_CACHE[key] = (dual, time.time())
        return dual

    # ── Leverage + margin mode ──
    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        # Parallel: marginType and leverage are independent endpoints. Doing
        # them concurrently shaves ~100-200ms off the first order on a symbol
        # (the usual latency of one API roundtrip to Binance Futures).
        sym = cls._symbol(symbol)

        async def _margin():
            try:
                await cls._signed(creds, "POST", "/fapi/v1/marginType",
                                  {"symbol": sym, "marginType": "ISOLATED" if margin_mode == "isolated" else "CROSSED"})
            except RuntimeError as e:
                if "-4046" not in str(e) and "No need" not in str(e):
                    raise RuntimeError(_friendly_error(*_split_code(e)))

        async def _lev():
            try:
                await cls._signed(creds, "POST", "/fapi/v1/leverage",
                                  {"symbol": sym, "leverage": int(leverage)})
            except RuntimeError as e:
                raise RuntimeError(_friendly_error(*_split_code(e)))

        await asyncio.gather(_margin(), _lev())

    # ── Pre-flight sanity ──
    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        """Non-destructive check. Returns {ok, qty_rounded, reason} — does not place an order."""
        sym = cls._symbol(symbol)
        info = (await _exchange_info()).get(sym)
        if not info:
            return {"ok": False, "reason": f"Symbol {sym} is not listed on Binance Futures."}

        step = info.get("stepSize") or 0
        min_qty = info.get("minQty") or 0
        min_notional = info.get("minNotional") or 0
        prec = info.get("quantityPrecision") or 8
        qty_r = _round_qty_to_step(quantity, step, prec)
        if qty_r <= 0 or qty_r < min_qty:
            return {"ok": False, "reason": f"Quantity below minimum ({min_qty} {symbol.upper()})."}

        # Mark-price estimate for notional check
        mark_price = None
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{BASE}/fapi/v1/premiumIndex?symbol={sym}")
                mark_price = float(r.json().get("markPrice") or 0)
        except Exception:
            pass
        if mark_price and min_notional and qty_r * mark_price < min_notional:
            return {"ok": False,
                    "reason": f"Notional below minimum (~${qty_r * mark_price:.2f} < ${min_notional:.2f}). "
                              f"Increase size or leverage."}

        # Balance vs required margin
        try:
            bal = (await cls.fetch_balance(creds)).get("usdt", 0)
        except RuntimeError as e:
            return {"ok": False, "reason": _friendly_error(*_split_code(e))}
        if mark_price and leverage > 0:
            required = (qty_r * mark_price) / max(1, leverage)
            if bal + 0.01 < required:
                return {"ok": False,
                        "reason": f"Insufficient margin: need ~${required:.2f} USDT, have ${bal:.2f}."}

        return {"ok": True, "qty_rounded": qty_r, "precision": prec,
                "min_qty": min_qty, "min_notional": min_notional, "step_size": step}

    # ── Place order ──
    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        sym = cls._symbol(symbol)
        info = (await _exchange_info()).get(sym) or {}
        step = info.get("stepSize") or 0
        prec = info.get("quantityPrecision") or 8
        qty_r = _round_qty_to_step(quantity, step, prec)
        qty_s = _qty_to_str(qty_r, prec)
        params: dict[str, Any] = {
            "symbol": sym,
            "side": "BUY" if side == "buy" else "SELL",
            "type": "MARKET",
            "quantity": qty_s,
        }
        # Hedge mode requires positionSide; in one-way leave unset
        if await cls._is_hedge_mode(creds):
            params["positionSide"] = "LONG" if side == "buy" else "SHORT"
        try:
            r = await cls._signed(creds, "POST", "/fapi/v1/order", params)
        except RuntimeError as e:
            raise RuntimeError(_friendly_error(*_split_code(e)))
        return {"order_id": str(r.get("orderId")), "avg_price": float(r.get("avgPrice", 0) or 0)}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        sym = cls._symbol(symbol)
        hedge = await cls._is_hedge_mode(creds)
        positions = await cls._signed(creds, "GET", "/fapi/v2/positionRisk", {"symbol": sym})
        # Pick the non-zero position (hedge mode returns two rows)
        target = None
        for p in positions:
            amt = float(p.get("positionAmt", 0) or 0)
            if amt != 0:
                target = p
                break
        if not target:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        amt = float(target.get("positionAmt", 0) or 0)
        reduce_side = "SELL" if amt > 0 else "BUY"
        info = (await _exchange_info()).get(sym) or {}
        prec = info.get("quantityPrecision") or 8
        qty_s = _qty_to_str(abs(amt), prec)
        params: dict[str, Any] = {
            "symbol": sym,
            "side": reduce_side,
            "type": "MARKET",
            "quantity": qty_s,
            "reduceOnly": "true",
        }
        if hedge:
            params["positionSide"] = target.get("positionSide") or ("LONG" if amt > 0 else "SHORT")
            params.pop("reduceOnly", None)  # positionSide handles reduce in hedge mode
        try:
            r = await cls._signed(creds, "POST", "/fapi/v1/order", params)
        except RuntimeError as e:
            raise RuntimeError(_friendly_error(*_split_code(e)))
        return {"order_id": str(r.get("orderId")), "closed_qty": abs(amt), "realized_pnl_usd": 0.0}

    # ── Positions ──
    # Per-user funding-PnL cache. Was: 1 call per position per list_positions
    # = N×30 weight × 6 polls/min = an instant 418 ban on Binance for any
    # user with >5 open positions. Now: 1 call per user per 30s, bucketed
    # in-memory by symbol. Drops weight cost by ~1/(N*6) for active /arb
    # users.
    _FUNDING_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
    _FUNDING_CACHE_TTL_S = 30.0

    @classmethod
    async def _funding_pnl_bulk(cls, creds: dict, since_ms: int) -> dict[str, float]:
        """Single bulk fetch: all FUNDING_FEE events for the account in
        one /fapi/v1/income call (no symbol filter). Returns {symbol: usd}.
        Cached 30s per api_key. Empty dict on failure (caller falls back
        to None per-symbol)."""
        api_key = (creds.get("api_key") or "").strip()
        cached = cls._FUNDING_CACHE.get(api_key)
        if cached and (time.time() - cached[0]) < cls._FUNDING_CACHE_TTL_S:
            return cached[1]
        try:
            data = await cls._signed(creds, "GET", "/fapi/v1/income", {
                "incomeType": "FUNDING_FEE",
                "startTime": since_ms,
                "limit": 1000,
            })
        except Exception as exc:
            logger.info("binance funding bulk fetch failed: %s", exc)
            return {}
        out: dict[str, float] = {}
        for ev in (data or []):
            sym = (ev.get("symbol") or "").upper()
            try:
                out[sym] = out.get(sym, 0.0) + float(ev.get("income") or 0)
            except (TypeError, ValueError):
                continue
        cls._FUNDING_CACHE[api_key] = (time.time(), out)
        return out

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        params = {"symbol": cls._symbol(symbol)} if symbol else None
        data = await cls._signed(creds, "GET", "/fapi/v2/positionRisk", params)
        positions = []
        for p in data:
            amt = float(p.get("positionAmt", 0) or 0)
            if amt == 0:
                continue
            mt = (p.get("marginType") or "").lower()
            margin_mode = "isolated" if mt.startswith("iso") else ("cross" if mt else None)
            positions.append({
                "exchange": "binance",
                "symbol": str(p.get("symbol", "")).replace("USDT", ""),
                "_api_symbol": p.get("symbol", ""),
                "side":   "buy" if amt > 0 else "sell",
                "quantity": abs(amt),
                "entry_price": float(p.get("entryPrice", 0) or 0),
                "mark_price":  float(p.get("markPrice",  0) or 0),
                "unrealized_pnl_usd": float(p.get("unRealizedProfit", 0) or 0),
                "leverage": int(float(p.get("leverage", 1) or 1)),
                "margin_mode": margin_mode,
                "position_id": str(p.get("symbol", "")),
            })
        if not positions:
            return []
        # Single bulk funding fetch. 7-day window covers most arb positions;
        # users can see precise accumulated funding on the exchange UI for
        # longer-held positions.
        since_ms = int((time.time() - 7 * 86400) * 1000)
        funding_by_sym = await cls._funding_pnl_bulk(creds, since_ms)
        for p in positions:
            api_sym = p.pop("_api_symbol", "")
            v = funding_by_sym.get((api_sym or "").upper())
            p["funding_pnl_usd"] = v if v is not None else None
        return positions

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["balance_usdt"] = float(bal.get("usdt") or 0)
        except Exception as e:
            msg = str(e)
            if "-2014" in msg or "Invalid API-key" in msg:
                out["error"] = "Invalid API key"
            elif "-2015" in msg or "permissions for action" in msg:
                out["error"] = "API key rejected by Binance (check key/IP whitelist/permissions)"
            elif "-1022" in msg or "Signature" in msg:
                out["error"] = "Signature mismatch — API secret is wrong"
            else:
                out["error"] = f"Binance rejected the key: {msg[:180]}"
            return out
        if need_trade:
            try:
                acct = await cls._signed(creds, "GET", "/fapi/v2/account")
                out["can_trade"] = bool(acct.get("canTrade"))
                if not out["can_trade"]:
                    out["error"] = "Key has no Futures trading permission"
            except Exception as e:
                out["error"] = f"Trade-permission probe failed: {str(e)[:180]}"
        return out

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        return 125


def _split_code(exc: Exception) -> tuple[str | None, str]:
    """Extract (code, msg) from 'Binance 400 -1111: ...'."""
    s = str(exc)
    # format: "Binance HTTP [CODE]: MSG"
    import re
    m = re.match(r"Binance \d+ (-?\d+)?: (.*)", s)
    if m:
        return m.group(1) or None, m.group(2) or s
    return None, s
