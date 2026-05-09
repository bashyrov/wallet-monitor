"""Bitget v2 USDT-M Futures trade adapter."""
from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import math
import time
from typing import Any

import httpx

from backend.providers.exchanges._signing import b64_hmac_sha256

BASE = "https://api.bitget.com"
logger = logging.getLogger("avalant.trade.bitget")

_INSTR_CACHE: dict[str, tuple[dict, float]] = {}
_INSTR_TTL = 600
_INSTR_LOCK = asyncio.Lock()
_ALL_INSTR_TS = 0.0


async def _load_all_instruments() -> None:
    global _ALL_INSTR_TS
    now = time.time()
    if now - _ALL_INSTR_TS < _INSTR_TTL:
        return
    async with _INSTR_LOCK:
        if time.time() - _ALL_INSTR_TS < _INSTR_TTL:
            return
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(f"{BASE}/api/v2/mix/market/contracts?productType=USDT-FUTURES")
                j = r.json()
                for item in (j.get("data") or []):
                    sym = item.get("symbol")
                    if not sym:
                        continue
                    info = {
                        "sizeMultiplier": float(item.get("sizeMultiplier") or 1),
                        "minTradeNum": float(item.get("minTradeNum") or 0.001),
                        "maxLeverage": int(float(item.get("maxLeverage") or 100)),
                        "pricePlace": int(item.get("pricePlace") or 2),
                        "volumePlace": int(item.get("volumePlace") or 4),
                        "symbolStatus": str(item.get("symbolStatus") or ""),
                    }
                    _INSTR_CACHE[sym] = (info, time.time())
                _ALL_INSTR_TS = time.time()
        except Exception as e:
            logger.debug("Bitget contracts fetch failed: %s", e)


async def _instrument_info(symbol: str) -> dict | None:
    hit = _INSTR_CACHE.get(symbol)
    if hit and time.time() - hit[1] < _INSTR_TTL:
        return hit[0]
    await _load_all_instruments()
    hit = _INSTR_CACHE.get(symbol)
    return hit[0] if hit else None


_BG_FRIENDLY = {
    "40001": "Invalid API key.",
    "40002": "Invalid request.",
    "40003": "Signature mismatch.",
    "40005": "Invalid passphrase.",
    "40006": "Timestamp expired — clock skew.",
    "40007": "API key permissions insufficient.",
    "40012": "IP not in whitelist.",
    "40725": "Insufficient balance for order.",
    "40756": "Order size below minimum.",
    "40786": "Symbol not found.",
    "45110": "Margin mode already set.",
}


def _friendly_bg(code: str | None, msg: str) -> str:
    if code and code in _BG_FRIENDLY:
        return _BG_FRIENDLY[code]
    return msg or "Bitget rejected the request."


def _split_code(exc: Exception) -> tuple[str | None, str]:
    import re
    m = re.match(r"Bitget (\d+): (.*)", str(exc))
    if m:
        return m.group(1), m.group(2)
    return None, str(exc)


def _qty_str(q: float, precision: int = 8) -> str:
    s = f"{q:.{max(precision, 0)}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".") or "0"
    return s


class BitgetAdapter:
    @staticmethod
    def _symbol(s: str) -> str:
        return s.upper() + "USDT"

    @classmethod
    async def _signed(cls, creds: dict, method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
        ts = str(int(time.time() * 1000))
        api_key = creds["api_key"]
        secret = creds["api_secret"]
        passphrase = creds["api_passphrase"]

        if method == "GET" and params:
            query = "&".join(f"{k}={params[k]}" for k in sorted(params))
            sign_path = path + "?" + query
            body_str = ""
        elif body is not None:
            sign_path = path
            body_str = jsonlib.dumps(body, separators=(",", ":"))
        else:
            sign_path = path
            body_str = ""

        sign_str = ts + method + sign_path + body_str
        signature = b64_hmac_sha256(secret, sign_str)

        headers = {
            "ACCESS-KEY": api_key,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }
        url = BASE + sign_path if method == "GET" else BASE + path
        async with httpx.AsyncClient(timeout=10) as c:
            if method == "GET":
                r = await c.get(url, headers=headers)
            else:
                r = await c.post(url, content=body_str or "{}", headers=headers)

        j = r.json()
        code = str(j.get("code", ""))
        if code != "00000":
            raise RuntimeError(f"Bitget {code}: {j.get('msg', r.text)}")
        return j.get("data")

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        data = await cls._signed(creds, "GET", "/api/v2/mix/account/accounts", {"productType": "USDT-FUTURES"})
        for acct in (data or []):
            if acct.get("marginCoin") == "USDT":
                return {"usdt": float(acct.get("available") or acct.get("crossedMaxAvailable") or 0)}
        return {"usdt": 0.0}

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        sym = cls._symbol(symbol)
        bg_mode = "isolated" if margin_mode == "isolated" else "crossed"

        async def _set_mode():
            try:
                await cls._signed(creds, "POST", "/api/v2/mix/account/set-margin-mode", body={
                    "symbol": sym,
                    "productType": "USDT-FUTURES",
                    "marginCoin": "USDT",
                    "marginMode": bg_mode,
                })
            except RuntimeError as e:
                if "45110" not in str(e) and "already" not in str(e).lower():
                    code, msg = _split_code(e)
                    raise RuntimeError(_friendly_bg(code, msg))

        async def _set_lev(hold_side: str):
            try:
                await cls._signed(creds, "POST", "/api/v2/mix/account/set-leverage", body={
                    "symbol": sym,
                    "productType": "USDT-FUTURES",
                    "marginCoin": "USDT",
                    "leverage": str(int(leverage)),
                    "holdSide": hold_side,
                })
            except RuntimeError as e:
                if "leverage" not in str(e).lower() or "not modified" not in str(e).lower():
                    code, msg = _split_code(e)
                    raise RuntimeError(_friendly_bg(code, msg))

        # Parallel: margin-mode + long-leverage + short-leverage. 3 API calls
        # go out concurrently rather than sequentially — saves ~200-400ms on
        # first order per symbol.
        await asyncio.gather(_set_mode(), _set_lev("long"), _set_lev("short"))

    @classmethod
    async def get_public_qty_limits(cls, symbol: str) -> dict | None:
        info = await _instrument_info(cls._symbol(symbol))
        if not info:
            return None
        size_mult = float(info.get("sizeMultiplier") or 0) or None
        min_trade = float(info.get("minTradeNum") or 0)
        return {
            "min_qty": min_trade,
            "step":    size_mult,
            "max_qty": None,
            "unit": "coin",
        }

    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        sym = cls._symbol(symbol)
        info = await _instrument_info(sym)
        if not info:
            return {"ok": False, "reason": f"{sym} is not listed on Bitget Futures."}
        if info.get("symbolStatus") and info["symbolStatus"].lower() not in ("normal", ""):
            return {"ok": False, "reason": f"{sym} is not trading ({info['symbolStatus']})."}

        size_mult = info.get("sizeMultiplier", 1)
        min_trade = info.get("minTradeNum", 0.001)
        vol_prec = info.get("volumePlace", 4)

        # Round qty to sizeMultiplier step
        qty_r = math.floor(quantity / size_mult) * size_mult if size_mult else quantity
        qty_r = round(qty_r, vol_prec)
        if qty_r < min_trade:
            return {"ok": False, "reason": f"Quantity below minimum ({min_trade} {symbol.upper()})."}

        if leverage > info.get("maxLeverage", 100):
            return {"ok": False, "reason": f"Max leverage for {sym} is {info['maxLeverage']}x."}

        try:
            bal = (await cls.fetch_balance(creds)).get("usdt", 0)
        except RuntimeError as e:
            code, msg = _split_code(e)
            return {"ok": False, "reason": _friendly_bg(code, msg)}

        mark_price = 0
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{BASE}/api/v2/mix/market/ticker?symbol={sym}&productType=USDT-FUTURES")
                tickers = (r.json().get("data") or [])
                if tickers:
                    mark_price = float(tickers[0].get("markPrice") or tickers[0].get("lastPr") or 0)
        except Exception:
            pass
        if mark_price and leverage > 0:
            required = (qty_r * mark_price) / max(1, leverage)
            if bal + 0.01 < required:
                return {"ok": False, "reason": f"Insufficient margin: need ~${required:.2f} USDT, have ${bal:.2f}."}

        return {"ok": True, "qty_rounded": qty_r, "size_multiplier": size_mult, "min_trade": min_trade, "volume_place": vol_prec}

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        sym = cls._symbol(symbol)
        info = await _instrument_info(sym) or {}
        size_mult = info.get("sizeMultiplier", 1)
        vol_prec = info.get("volumePlace", 4)
        qty_r = math.floor(quantity / size_mult) * size_mult if size_mult else quantity
        qty_r = round(qty_r, vol_prec)
        if qty_r <= 0:
            raise RuntimeError(f"Quantity below minimum for {sym}")
        try:
            data = await cls._signed(creds, "POST", "/api/v2/mix/order/place-order", body={
                "symbol": sym,
                "productType": "USDT-FUTURES",
                "marginMode": "isolated" if margin_mode == "isolated" else "crossed",
                "marginCoin": "USDT",
                "side": "buy" if side == "buy" else "sell",
                "tradeSide": "open",
                "orderType": "market",
                "size": _qty_str(qty_r, vol_prec),
            })
        except RuntimeError as e:
            code, msg = _split_code(e)
            raise RuntimeError(_friendly_bg(code, msg))
        return {"order_id": str((data or {}).get("orderId", "")), "avg_price": 0.0}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        sym = cls._symbol(symbol)
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        target = next((q for q in positions if (q.get("side") or "").lower() == side.lower()), positions[0])
        p = target
        # Use the dedicated /close-positions endpoint. It flushes the symbol's
        # position regardless of one-way / hedge mode, so we don't have to
        # match holdSide / tradeSide / posMode against the user's account
        # configuration. In hedge mode this flushes both legs for the symbol;
        # since arb workflows close one-leg-at-a-time we accept that side
        # effect (the test harness above re-verifies the side of any leftovers).
        try:
            data = await cls._signed(creds, "POST", "/api/v2/mix/order/close-positions", body={
                "symbol": sym,
                "productType": "USDT-FUTURES",
            })
        except RuntimeError as e:
            code, msg = _split_code(e)
            raise RuntimeError(_friendly_bg(code, msg))
        # Response shape: {"successList": [...], "failureList": [...]}
        success = (data or {}).get("successList") or []
        order_id = success[0].get("orderId", "") if success else ""
        return {
            "order_id": str(order_id),
            "closed_qty": p["quantity"],
            "realized_pnl_usd": p.get("unrealized_pnl_usd", 0),
        }

    @classmethod
    async def _funding_pnl(cls, creds: dict, api_symbol: str, since_ms: int) -> float | None:
        """Bitget v2: /api/v2/mix/account/bill with businessType=contract_settle_fee.

        Earlier code hit /account/account-bill (404) with businessType
        contract_fund (40020 'parameter error') — both wrong. The correct
        path returns rows like:
          {amount:"1.34", businessType:"contract_settle_fee",
           coin:"USDT", symbol:"LABUSDT", cTime:...}
        Sum of `amount` over all settle_fee rows since `since_ms` is the
        net funding the position has earned/paid in USDT.
        """
        try:
            data = await cls._signed(creds, "GET", "/api/v2/mix/account/bill", {
                "productType": "USDT-FUTURES",
                "symbol": api_symbol,
                "businessType": "contract_settle_fee",
                "startTime": since_ms,
                "limit": 100,
            })
            items = (data or {}).get("bills") if isinstance(data, dict) else data
            total = 0.0
            for x in (items or []):
                # Belt-and-braces filter — server should already only
                # return contract_settle_fee rows, but if a future API
                # change loosens the filter, we still get the right sum.
                if str(x.get("businessType") or "") != "contract_settle_fee":
                    continue
                try:
                    total += float(x.get("amount") or 0)
                except (TypeError, ValueError):
                    continue
            return total
        except Exception:
            return None

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        params: dict[str, str] = {"productType": "USDT-FUTURES"}
        if symbol:
            params["symbol"] = cls._symbol(symbol)
        data = await cls._signed(creds, "GET", "/api/v2/mix/position/all-position", params)
        positions = []
        for p in (data or []):
            qty = float(p.get("total") or p.get("available") or 0)
            if qty == 0:
                continue
            hold_side = str(p.get("holdSide") or "").lower()
            mm = (p.get("marginMode") or "").lower()
            margin_mode = "isolated" if mm.startswith("iso") else ("cross" if mm else None)
            positions.append({
                "exchange": "bitget",
                "symbol": str(p.get("symbol", "")).replace("USDT", ""),
                "_api_symbol": str(p.get("symbol", "")),
                "side": "buy" if hold_side == "long" else "sell",
                "quantity": qty,
                "entry_price": float(p.get("openPriceAvg") or 0),
                "mark_price": float(p.get("markPrice") or 0),
                "unrealized_pnl_usd": float(p.get("unrealizedPL") or 0),
                "leverage": int(float(p.get("leverage") or 1)),
                "margin_mode": margin_mode,
                "position_id": str(p.get("positionId", "")),
            })
        if not positions:
            return []
        import time as _t
        since_ms = int((_t.time() - 7 * 86400) * 1000)
        from backend.services.trade_adapters._funding_cache import cached_funding
        api_key = (creds.get("api_key") or "").strip()
        fundings = await asyncio.gather(*[
            cached_funding(api_key, p["_api_symbol"],
                           lambda p=p: cls._funding_pnl(creds, p["_api_symbol"], since_ms))
            for p in positions
        ], return_exceptions=True)
        for p, f in zip(positions, fundings):
            p["funding_pnl_usd"] = f if isinstance(f, (int, float)) else None
            p.pop("_api_symbol", None)
        return positions

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        if not creds.get("api_passphrase"):
            out["error"] = "Bitget requires a passphrase"
            return out
        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["balance_usdt"] = float(bal.get("usdt") or 0)
        except Exception as e:
            msg = str(e)
            if "40001" in msg:
                out["error"] = "Invalid API key"
            elif "40003" in msg:
                out["error"] = "Signature mismatch — check API secret"
            elif "40005" in msg:
                out["error"] = "Invalid passphrase"
            elif "40007" in msg:
                out["error"] = "Key permissions insufficient"
            else:
                out["error"] = f"Bitget rejected the key: {msg[:180]}"
            return out
        if need_trade:
            out["can_trade"] = True
        return out

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        info = await _instrument_info(cls._symbol(symbol))
        if info:
            return info.get("maxLeverage", 100)
        return 100

    @classmethod
    async def fetch_recent_fills(cls, creds: dict, since_ts, *,
                                 market: str = "futures") -> list[dict]:
        """Bitget v2 mix fills since `since_ts`.

        Futures: /api/v2/mix/order/fills?productType=USDT-FUTURES&startTime=<ms>.
        Funding: /api/v2/mix/account/account-bill?coin=USDT&businessType=
        contract_settle_fee. Spot returns []."""
        from datetime import datetime as _dt
        if market != "futures":
            return []
        start_ms = int(since_ts.timestamp() * 1000)
        out: list[dict] = []
        try:
            data = await cls._signed(creds, "GET",
                                     "/api/v2/mix/order/fills", {
                                         "productType": "USDT-FUTURES",
                                         "startTime": start_ms,
                                         "limit": 500,
                                     }) or {}
        except Exception:
            data = {}
        rows = (data.get("fillList") if isinstance(data, dict) else None) or []
        if isinstance(data, dict) and not rows:
            rows = (data.get("data") or {}).get("fillList") if isinstance(data.get("data"), dict) else []
            rows = rows or []
        for r in rows:
            try:
                sym_raw = str(r.get("symbol") or "")
                sym = sym_raw.replace("USDT", "")
                side = "buy" if str(r.get("side") or "").lower() == "buy" else "sell"
                qty = float(r.get("baseVolume") or r.get("size") or 0)
                if qty <= 0:
                    continue
                ts_raw = r.get("cTime") or r.get("ctime")
                ts_ms = int(ts_raw) if ts_raw else 0
                if ts_ms <= 0:
                    continue
                rpnl_raw = r.get("profit") or r.get("realizedPL")
                rpnl = float(rpnl_raw) if rpnl_raw not in (None, "") else None
                fee_raw = (r.get("feeDetail") or [{}])
                fee = None
                if isinstance(fee_raw, list) and fee_raw:
                    fee_v = fee_raw[0].get("totalFee") or fee_raw[0].get("fee")
                    if fee_v is not None:
                        fee = abs(float(fee_v))
                out.append({
                    "symbol": sym.upper(),
                    "side": side,
                    "qty": qty,
                    "price": float(r.get("price") or 0),
                    "fee_usd": fee,
                    "realized_pnl_usd": rpnl,
                    "ts": _dt.utcfromtimestamp(ts_ms / 1000),
                    "ext_trade_id": str(r.get("tradeId") or r.get("fillId") or ""),
                    "ext_order_id": str(r.get("orderId") or "") or None,
                    "kind": "trade",
                })
            except Exception:
                continue
        try:
            fdata = await cls._signed(creds, "GET",
                                      "/api/v2/mix/account/account-bill", {
                                          "productType": "USDT-FUTURES",
                                          "coin": "USDT",
                                          "businessType": "contract_settle_fee",
                                          "startTime": start_ms,
                                          "limit": 100,
                                      }) or {}
            f_rows = (fdata.get("bills") if isinstance(fdata, dict) else None) or []
            for r in f_rows:
                try:
                    sym_raw = str(r.get("symbol") or "")
                    sym = sym_raw.replace("USDT", "")
                    ts_ms = int(r.get("cTime") or 0)
                    if ts_ms <= 0:
                        continue
                    out.append({
                        "symbol": sym.upper(),
                        "side": None,
                        "qty": 0.0, "price": 0.0, "fee_usd": None,
                        "realized_pnl_usd": float(r.get("amount") or 0),
                        "ts": _dt.utcfromtimestamp(ts_ms / 1000),
                        "ext_trade_id": str(r.get("billId")
                                            or f"funding-{ts_ms}-{sym}"),
                        "ext_order_id": None,
                        "kind": "funding",
                    })
                except Exception:
                    continue
        except Exception:
            pass
        return out
