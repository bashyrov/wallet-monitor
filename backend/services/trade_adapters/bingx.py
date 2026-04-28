"""BingX USDT-M Perpetual Swap trade adapter."""
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

BASE = "https://open-api.bingx.com"
logger = logging.getLogger("avalant.trade.bingx")

# ── Instrument cache ──
_EX_INFO_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_EX_INFO_TTL = 600
_EX_INFO_LOCK = asyncio.Lock()


async def _exchange_info() -> dict[str, dict]:
    """Return {symbol: {quantityPrecision, minQty, stepSize, tickSize}}."""
    now = time.time()
    if _EX_INFO_CACHE["data"] and now - _EX_INFO_CACHE["ts"] < _EX_INFO_TTL:
        return _EX_INFO_CACHE["data"]
    async with _EX_INFO_LOCK:
        if _EX_INFO_CACHE["data"] and time.time() - _EX_INFO_CACHE["ts"] < _EX_INFO_TTL:
            return _EX_INFO_CACHE["data"]
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{BASE}/openApi/swap/v2/quote/contracts")
                body = r.json()
        except Exception as e:
            logger.warning("BingX exchangeInfo failed: %s", e)
            return _EX_INFO_CACHE["data"] or {}
        out: dict[str, dict] = {}
        for s in body.get("data", []):
            sym = s.get("symbol")
            if not sym:
                continue
            out[sym] = {
                "quantityPrecision": int(s.get("quantityPrecision", 2) or 2),
                "pricePrecision": int(s.get("pricePrecision", 2) or 2),
                "minQty": float(s.get("minTradeQuantity") or s.get("tradeMinQuantity") or 0),
                "stepSize": float(s.get("stepSize") or 0),
                "tickSize": float(s.get("tickSize") or 0),
            }
        _EX_INFO_CACHE["data"] = out
        _EX_INFO_CACHE["ts"] = time.time()
        return out


_FRIENDLY = {
    "100001": "Signature mismatch — check API secret.",
    "100202": "Insufficient margin.",
    "100400": "Invalid parameter.",
    "100410": "Symbol not listed on BingX Futures.",
    "100421": "Leverage value not allowed.",
    "100500": "Internal server error on BingX.",
    "80001":  "Invalid API key.",
    "80012":  "API key has no trade permission.",
}


def _friendly_error(code: str | None, msg: str) -> str:
    if code and code in _FRIENDLY:
        return _FRIENDLY[code]
    return msg or "BingX rejected the request."


def _round_qty(qty: float, step: float | None, prec: int) -> float:
    if step and step > 0:
        return math.floor(qty / step) * step
    factor = 10 ** prec
    return math.floor(qty * factor) / factor


def _qty_str(qty: float, prec: int) -> str:
    s = f"{qty:.{max(prec, 0)}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".") or "0"
    return s


class BingxAdapter:
    @staticmethod
    def _sign(params: dict, secret: str) -> str:
        sorted_qs = urllib.parse.urlencode(sorted(params.items()), doseq=True)
        return hmac.new(secret.encode(), sorted_qs.encode(), hashlib.sha256).hexdigest()

    @classmethod
    async def _req(cls, creds: dict, method: str, path: str, params: dict | None = None) -> Any:
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        sig = cls._sign(params, creds["api_secret"])
        # Build the query string EXACTLY the same way we signed it — passing
        # params= to httpx may URL-encode differently (e.g. + vs %2B,
        # comma vs %2C) which corrupts the signature. Append the query
        # string to the URL ourselves so the bytes on the wire match what
        # we hashed.
        sorted_qs = urllib.parse.urlencode(sorted(params.items()), doseq=True)
        sorted_qs += f"&signature={sig}"
        headers = {"X-BX-APIKEY": creds["api_key"]}
        url = f"{BASE}{path}?{sorted_qs}"
        async with httpx.AsyncClient(timeout=10) as c:
            if method == "GET":
                r = await c.get(url, headers=headers)
            else:
                r = await c.post(url, headers=headers)
            body = r.json()
            code = str(body.get("code", 0))
            if code != "0" and r.status_code >= 400 or code not in ("0", "200"):
                msg = str(body.get("msg") or r.text)
                raise RuntimeError(f"BingX {r.status_code} {code}: {msg}")
            return body.get("data", body)

    @staticmethod
    def _symbol(s: str) -> str:
        return s.upper() + "-USDT"

    # ── Balance ──
    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        data = await cls._req(creds, "GET", "/openApi/swap/v2/user/balance")
        bal = data.get("balance", data) if isinstance(data, dict) else {}
        return {"usdt": float(bal.get("availableMargin") or bal.get("equity") or 0)}

    # ── Leverage + margin mode ──
    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        sym = cls._symbol(symbol)

        async def _mode():
            try:
                await cls._req(creds, "POST", "/openApi/swap/v2/trade/marginType", {
                    "symbol": sym,
                    "marginType": "ISOLATED" if margin_mode == "isolated" else "CROSSED",
                })
            except RuntimeError:
                pass  # already set

        async def _lev_side(s: str):
            try:
                await cls._req(creds, "POST", "/openApi/swap/v2/trade/leverage", {
                    "symbol": sym, "side": s, "leverage": int(leverage),
                })
            except RuntimeError as e:
                # 80012 / 109400 "leverage not modified" — fine
                if not any(c in str(e) for c in ("80012", "109400", "100413")):
                    raise

        async def _lev():
            # Try BOTH first (one-way mode); on hedge-mode rejection, set
            # both LONG and SHORT separately. BingX errors with code 100400
            # "side error" if BOTH is sent in hedge mode.
            try:
                await cls._req(creds, "POST", "/openApi/swap/v2/trade/leverage", {
                    "symbol": sym, "side": "BOTH", "leverage": int(leverage),
                })
            except RuntimeError as e:
                s = str(e)
                if any(t in s for t in ("hedge", "Hedge", "side", "100400", "LONG", "SHORT")):
                    # Hedge mode — set both legs
                    await asyncio.gather(_lev_side("LONG"), _lev_side("SHORT"),
                                          return_exceptions=True)
                else:
                    if not any(c in s for c in ("80012", "109400")):
                        raise RuntimeError(_friendly_error(*_split_code(e)))

        await asyncio.gather(_mode(), _lev())

    # ── Preflight ──
    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        sym = cls._symbol(symbol)
        info = (await _exchange_info()).get(sym)
        if not info:
            return {"ok": False, "reason": f"Symbol {sym} not listed on BingX Futures."}
        prec = info.get("quantityPrecision", 2)
        step = info.get("stepSize") or 0
        min_qty = info.get("minQty") or 0
        qty_r = _round_qty(quantity, step, prec)
        if qty_r <= 0 or qty_r < min_qty:
            return {"ok": False, "reason": f"Quantity below minimum ({min_qty} {symbol.upper()})."}
        try:
            bal = (await cls.fetch_balance(creds)).get("usdt", 0)
        except RuntimeError as e:
            return {"ok": False, "reason": _friendly_error(*_split_code(e))}
        return {"ok": True, "qty_rounded": qty_r, "precision": prec,
                "min_qty": min_qty, "step_size": step}

    # ── Place order ──
    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        sym = cls._symbol(symbol)
        info = (await _exchange_info()).get(sym) or {}
        prec = info.get("quantityPrecision", 2)
        step = info.get("stepSize") or 0
        qty_r = _round_qty(quantity, step, prec)
        qty_s = _qty_str(qty_r, prec)
        # Hedge-mode accounts require positionSide=LONG/SHORT; one-way
        # accounts ignore it. Sending it in both modes is safe.
        position_side = "LONG" if side == "buy" else "SHORT"
        try:
            r = await cls._req(creds, "POST", "/openApi/swap/v2/trade/order", {
                "symbol": sym,
                "type": "MARKET",
                "side": "BUY" if side == "buy" else "SELL",
                "positionSide": position_side,
                "quantity": qty_s,
            })
        except RuntimeError as e:
            raise RuntimeError(_friendly_error(*_split_code(e)))
        order = r if isinstance(r, dict) else {}
        return {"order_id": str(order.get("orderId", "")), "avg_price": float(order.get("avgPrice", 0) or 0)}

    # ── Close position ──
    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        sym = cls._symbol(symbol)
        positions = await cls._req(creds, "GET", "/openApi/swap/v2/user/positions", {"symbol": sym})
        pos_list = positions if isinstance(positions, list) else []
        # In hedge mode `positionSide` is "LONG"/"SHORT". Match the requested
        # side rather than picking the first non-zero leg, otherwise we'd
        # close the wrong direction in a paired arb position.
        target = None
        want_pside = "LONG" if side.lower() == "buy" else "SHORT"
        for p in pos_list:
            amt = float(p.get("positionAmt") or p.get("availableAmt") or 0)
            if amt == 0:
                continue
            ps = (p.get("positionSide") or "").upper()
            if ps == want_pside:
                target = p
                break
        # Fallback: any non-zero leg (one-way mode where positionSide="BOTH")
        if target is None:
            for p in pos_list:
                amt = float(p.get("positionAmt") or p.get("availableAmt") or 0)
                if amt != 0:
                    target = p
                    break
        if not target:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        amt = float(target.get("positionAmt") or target.get("availableAmt") or 0)
        position_side = (target.get("positionSide") or "BOTH").upper()
        # In HEDGE mode positionAmt is always positive (magnitude held);
        # direction is encoded in positionSide. To close LONG you SELL,
        # to close SHORT you BUY. In ONE-WAY (positionSide=BOTH) the sign
        # of positionAmt encodes direction.
        if position_side == "LONG":
            reduce_side = "SELL"
        elif position_side == "SHORT":
            reduce_side = "BUY"
        else:
            reduce_side = "SELL" if amt > 0 else "BUY"
        info = (await _exchange_info()).get(sym) or {}
        prec = info.get("quantityPrecision", 2)
        qty_s = _qty_str(abs(amt), prec)
        # Hedge mode (positionSide=LONG/SHORT): BingX rejects reduceOnly.
        #   "In the Hedge mode, the 'ReduceOnly' field can not be filled."
        # The positionSide already disambiguates which leg to flatten, so
        # reduceOnly is redundant. One-way mode (positionSide=BOTH) accepts
        # reduceOnly. Detect mode and adjust.
        is_hedge = position_side in ("LONG", "SHORT")
        body = {
            "symbol": sym,
            "type": "MARKET",
            "side": reduce_side,
            "positionSide": position_side,
            "quantity": qty_s,
        }
        if not is_hedge:
            body["reduceOnly"] = "true"
        try:
            r = await cls._req(creds, "POST", "/openApi/swap/v2/trade/order", body)
        except RuntimeError as e:
            raise RuntimeError(_friendly_error(*_split_code(e)))
        return {"order_id": str((r or {}).get("orderId", "")), "closed_qty": abs(amt), "realized_pnl_usd": 0.0}

    # ── Positions ──
    @classmethod
    async def _funding_pnl(cls, creds: dict, api_symbol: str, since_ms: int) -> float | None:
        """Sum `amount` from /openApi/swap/v2/user/income with incomeType='FUNDING_FEE'.
        Returns None on failure so the UI shows '—' instead of a misleading zero."""
        try:
            data = await cls._req(creds, "GET", "/openApi/swap/v2/user/income", {
                "symbol": api_symbol,
                "incomeType": "FUNDING_FEE",
                "startTime": since_ms,
                "limit": 1000,
            })
            return sum(float(x.get("income") or x.get("amount") or 0) for x in (data or []))
        except Exception:
            return None

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        import time as _t
        params = {"symbol": cls._symbol(symbol)} if symbol else {}
        data = await cls._req(creds, "GET", "/openApi/swap/v2/user/positions", params or None)
        pos_list = data if isinstance(data, list) else []
        positions = []
        for p in pos_list:
            amt = float(p.get("positionAmt") or 0)
            if amt == 0:
                continue
            sym_raw = str(p.get("symbol", ""))
            # In hedge mode positionAmt is always positive, direction comes
            # from positionSide. Reflect that in side parsing.
            ps = (p.get("positionSide") or "").upper()
            if ps == "LONG":
                side_parsed = "buy"
            elif ps == "SHORT":
                side_parsed = "sell"
            else:
                side_parsed = "buy" if amt > 0 else "sell"
            iso_flag = p.get("isolated")
            if iso_flag is None:
                margin_mode = None
            else:
                margin_mode = "isolated" if bool(iso_flag) else "cross"
            positions.append({
                "exchange": "bingx",
                "symbol": sym_raw.replace("-USDT", ""),
                "_api_symbol": sym_raw,
                "side": side_parsed,
                "quantity": abs(amt),
                "entry_price": float(p.get("avgPrice") or 0),
                "mark_price": float(p.get("markPrice") or 0),
                "unrealized_pnl_usd": float(p.get("unrealizedProfit") or 0),
                "leverage": int(float(p.get("leverage") or 1)),
                "margin_mode": margin_mode,
                "position_id": sym_raw,
            })
        if not positions:
            return []
        since_ms = int((_t.time() - 7 * 86400) * 1000)
        fundings = await asyncio.gather(*[
            cls._funding_pnl(creds, p["_api_symbol"], since_ms) for p in positions
        ], return_exceptions=True)
        for p, f in zip(positions, fundings):
            p["funding_pnl_usd"] = f if isinstance(f, (int, float)) else None
            p.pop("_api_symbol", None)
        return positions

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
            if "80001" in msg:
                out["error"] = "Invalid API key"
            elif "100001" in msg or "Signature" in msg:
                out["error"] = "Signature mismatch — API secret is wrong"
            else:
                out["error"] = f"BingX rejected the key: {msg[:180]}"
            return out
        if need_trade:
            out["can_trade"] = True  # BingX doesn't have a separate trade permission check
        return out

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        return 150


def _split_code(exc: Exception) -> tuple[str | None, str]:
    import re
    s = str(exc)
    m = re.match(r"BingX \d+ (\d+)?: (.*)", s)
    if m:
        return m.group(1) or None, m.group(2) or s
    return None, s
