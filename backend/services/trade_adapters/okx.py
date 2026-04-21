"""OKX USDT-M Swap trade adapter."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json as jsonlib
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any

import httpx

BASE = "https://www.okx.com"
logger = logging.getLogger("avalant.trade.okx")

_INSTR_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_INSTR_TTL = 600
_INSTR_LOCK = asyncio.Lock()


async def _instruments() -> dict[str, dict]:
    """Return {instId: {lotSz, minSz, tickSz, ctVal, lever}}."""
    now = time.time()
    if _INSTR_CACHE["data"] and now - _INSTR_CACHE["ts"] < _INSTR_TTL:
        return _INSTR_CACHE["data"]
    async with _INSTR_LOCK:
        if _INSTR_CACHE["data"] and time.time() - _INSTR_CACHE["ts"] < _INSTR_TTL:
            return _INSTR_CACHE["data"]
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{BASE}/api/v5/public/instruments?instType=SWAP")
                items = r.json().get("data") or []
        except Exception as e:
            logger.warning("OKX instruments fetch failed: %s", e)
            return _INSTR_CACHE["data"] or {}
        out: dict[str, dict] = {}
        for it in items:
            iid = it.get("instId", "")
            if not iid.endswith("-USDT-SWAP"):
                continue
            out[iid] = {
                "lotSz": float(it.get("lotSz") or 1),
                "minSz": float(it.get("minSz") or 1),
                "tickSz": float(it.get("tickSz") or 0.01),
                "ctVal": float(it.get("ctVal") or 1),
                "lever": int(float(it.get("lever") or 125)),
            }
        _INSTR_CACHE["data"] = out
        _INSTR_CACHE["ts"] = time.time()
        return out


_OKX_FRIENDLY = {
    "50000": "Bad request body.",
    "50001": "API key does not match current environment.",
    "50002": "OKX rejected the request — check timestamp.",
    "50004": "Endpoint requires a higher API key permission level.",
    "50011": "Rate limit exceeded — try again in a moment.",
    "50013": "System busy — try again.",
    "50014": "Invalid API key.",
    "50026": "System error — try again.",
    "50111": "Invalid account — check API key scope.",
    "51000": "Parameter error.",
    "51001": "Instrument does not exist on OKX.",
    "51008": "Insufficient balance.",
    "51010": "Account mismatch — key may not have trade permission.",
    "51020": "Order size below minimum.",
    "51100": "Trading account is not activated.",
    "51101": "Too many open orders.",
    "51109": "Order not exist.",
    "51113": "Close position order size exceeds position.",
    "51115": "Leverage cannot exceed the exchange max.",
    "51116": "Cancel order would cause position liquidation.",
    "59000": "Margin mode cannot be changed while holding positions.",
    "59001": "Margin mode already set.",
}


def _friendly_okx(code: str | None, msg: str) -> str:
    if code and code in _OKX_FRIENDLY:
        return _OKX_FRIENDLY[code]
    return msg or "OKX rejected the request."


def _split_code(exc: Exception) -> tuple[str | None, str]:
    import re
    s = str(exc)
    m = re.match(r"OKX (\d+): (.*)", s)
    if m:
        return m.group(1), m.group(2)
    return None, s


def _round_qty_to_step(qty: float, step: float) -> float:
    if step > 0:
        return math.floor(qty / step) * step
    return qty


def _qty_to_str(qty: float) -> str:
    s = f"{qty:.8f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".") or "0"
    return s


def _okx_symbol(s: str) -> str:
    return s.upper() + "-USDT-SWAP"


class OKXAdapter:
    @staticmethod
    def _ts() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    @staticmethod
    def _sign(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
        pre = timestamp + method.upper() + path + body
        digest = hmac.new(secret.encode(), pre.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    @classmethod
    async def _req(cls, creds: dict, method: str, path: str, body: dict | None = None) -> Any:
        ts = cls._ts()
        body_str = jsonlib.dumps(body, separators=(",", ":")) if body else ""
        sig = cls._sign(creds["api_secret"], ts, method, path, body_str)
        headers = {
            "OK-ACCESS-KEY": creds["api_key"],
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": creds["api_passphrase"],
            "Content-Type": "application/json",
        }
        url = BASE + path
        async with httpx.AsyncClient(timeout=10) as c:
            if method == "GET":
                r = await c.get(url, headers=headers)
            else:
                r = await c.post(url, headers=headers, content=body_str)
        if r.status_code >= 400:
            raise RuntimeError(f"OKX HTTP {r.status_code}: {r.text[:300]}")
        j = r.json()
        code = str(j.get("code", "0"))
        if code != "0":
            msg = j.get("msg") or ""
            # some endpoints put error in data[0].sMsg
            if j.get("data") and isinstance(j["data"], list) and j["data"]:
                sub = j["data"][0]
                msg = msg or sub.get("sMsg") or sub.get("msg") or ""
                code = sub.get("sCode") or code
            raise RuntimeError(f"OKX {code}: {msg}")
        return j.get("data") or []

    # ── Balance ──
    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        data = await cls._req(creds, "GET", "/api/v5/account/balance")
        for acct in data:
            for d in acct.get("details", []):
                if d.get("ccy") == "USDT":
                    return {"usdt": float(d.get("availBal") or d.get("cashBal") or 0)}
        return {"usdt": 0.0}

    # ── Leverage + margin mode ──
    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        inst_id = _okx_symbol(symbol)
        mgn = "isolated" if margin_mode == "isolated" else "cross"
        # Set position mode to long_short_mode (hedge) — ignore if already set
        try:
            await cls._req(creds, "POST", "/api/v5/account/set-position-mode", {"posMode": "long_short_mode"})
        except RuntimeError:
            pass
        # Set margin mode via set-leverage (OKX sets margin mode per instrument with leverage call)
        try:
            await cls._req(creds, "POST", "/api/v5/account/set-leverage", {
                "instId": inst_id,
                "lever": str(int(leverage)),
                "mgnMode": mgn,
            })
        except RuntimeError as e:
            s = str(e)
            if "59001" not in s and "59000" not in s:
                raise RuntimeError(_friendly_okx(*_split_code(e)))

    # ── Preflight ──
    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        inst_id = _okx_symbol(symbol)
        instruments = await _instruments()
        info = instruments.get(inst_id)
        if not info:
            return {"ok": False, "reason": f"{inst_id} is not listed on OKX."}

        ct_val = info["ctVal"]
        lot_sz = info["lotSz"]
        min_sz = info["minSz"]

        # OKX sz is in contracts; 1 contract = ctVal coins
        # Convert coin quantity to contracts
        contracts = quantity / ct_val if ct_val > 0 else quantity
        contracts = _round_qty_to_step(contracts, lot_sz)
        if contracts < min_sz:
            return {"ok": False, "reason": f"Quantity below minimum ({min_sz} contracts = {min_sz * ct_val} {symbol.upper()})."}

        # Mark price
        mark_price = 0.0
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{BASE}/api/v5/public/mark-price?instType=SWAP&instId={inst_id}")
                d = r.json().get("data") or []
                if d:
                    mark_price = float(d[0].get("markPx") or 0)
        except Exception:
            pass

        # Balance check
        try:
            bal = (await cls.fetch_balance(creds)).get("usdt", 0)
        except RuntimeError as e:
            return {"ok": False, "reason": _friendly_okx(*_split_code(e))}

        notional = contracts * ct_val * mark_price
        if mark_price and leverage > 0:
            required = notional / max(1, leverage)
            if bal + 0.01 < required:
                return {"ok": False, "reason": f"Insufficient margin: need ~${required:.2f} USDT, have ${bal:.2f}."}

        return {
            "ok": True,
            "qty_rounded": contracts * ct_val,
            "contracts": contracts,
            "ct_val": ct_val,
            "lot_sz": lot_sz,
            "min_sz": min_sz,
        }

    # ── Place order ──
    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        inst_id = _okx_symbol(symbol)
        instruments = await _instruments()
        info = instruments.get(inst_id) or {}
        ct_val = info.get("ctVal", 1)
        lot_sz = info.get("lotSz", 1)

        contracts = quantity / ct_val if ct_val > 0 else quantity
        contracts = _round_qty_to_step(contracts, lot_sz)
        if contracts <= 0:
            raise RuntimeError(f"Quantity too small for {inst_id}.")

        try:
            r = await cls._req(creds, "POST", "/api/v5/trade/order", {
                "instId": inst_id,
                "tdMode": "isolated" if margin_mode == "isolated" else "cross",
                "side": "buy" if side == "buy" else "sell",
                "posSide": "long" if side == "buy" else "short",
                "ordType": "market",
                "sz": _qty_to_str(contracts),
            })
        except RuntimeError as e:
            raise RuntimeError(_friendly_okx(*_split_code(e)))
        order_id = r[0].get("ordId", "") if r else ""
        # Fetch fill price
        avg_price = 0.0
        if order_id:
            try:
                await asyncio.sleep(0.3)
                fills = await cls._req(creds, "GET", f"/api/v5/trade/order?instId={inst_id}&ordId={order_id}")
                if fills:
                    avg_price = float(fills[0].get("avgPx") or 0)
            except Exception:
                pass
        return {"order_id": str(order_id), "avg_price": avg_price}

    # ── Close position ──
    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        inst_id = _okx_symbol(symbol)
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        p = positions[0]
        close_side = "sell" if p["side"] == "buy" else "buy"
        pos_side = "long" if p["side"] == "buy" else "short"
        # Match the position's own margin mode — closing in a different mode
        # would be rejected by OKX ("52000: Position & order tdMode mismatch").
        td_mode = p.get("margin_mode") or "isolated"
        td_mode = "isolated" if td_mode.lower().startswith("iso") else "cross"

        instruments = await _instruments()
        info = instruments.get(inst_id) or {}
        ct_val = info.get("ctVal", 1)
        contracts = p["quantity"] / ct_val if ct_val > 0 else p["quantity"]

        try:
            r = await cls._req(creds, "POST", "/api/v5/trade/order", {
                "instId": inst_id,
                "tdMode": td_mode,
                "side": close_side,
                "posSide": pos_side,
                "ordType": "market",
                "sz": _qty_to_str(contracts),
                "reduceOnly": True,
            })
        except RuntimeError as e:
            raise RuntimeError(_friendly_okx(*_split_code(e)))
        order_id = r[0].get("ordId", "") if r else ""
        return {"order_id": str(order_id), "closed_qty": p["quantity"], "realized_pnl_usd": p.get("unrealized_pnl_usd", 0)}

    # ── Positions ──
    @classmethod
    async def _funding_pnl(cls, creds: dict, inst_id: str, since_ms: int) -> float | None:
        """Sum funding bills for `inst_id`. OKX type=8 = funding fee.
        /api/v5/account/bills-archive covers >3 months; /bills covers recent
        3 months. We use /bills since arb positions are usually short-lived."""
        try:
            path = f"/api/v5/account/bills?instType=SWAP&type=8&instId={inst_id}&begin={since_ms}&limit=100"
            data = await cls._req(creds, "GET", path)
            return sum(float(x.get("balChg") or x.get("pnl") or 0) for x in (data or []))
        except Exception:
            return None

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        path = "/api/v5/account/positions?instType=SWAP"
        if symbol:
            path += f"&instId={_okx_symbol(symbol)}"
        data = await cls._req(creds, "GET", path)
        positions = []
        for p in data:
            pos = float(p.get("pos") or 0)
            if pos == 0:
                continue
            ct_val = float(p.get("ctVal") or 1)
            qty_coins = abs(pos) * ct_val
            ps = p.get("posSide", "")
            if ps == "long":
                side = "buy"
            elif ps == "short":
                side = "sell"
            else:
                side = "buy" if pos > 0 else "sell"
            inst_id = p.get("instId", "")
            sym = inst_id.replace("-USDT-SWAP", "")
            positions.append({
                "exchange": "okx",
                "symbol": sym,
                "_inst_id": inst_id,
                "side": side,
                "quantity": qty_coins,
                "entry_price": float(p.get("avgPx") or 0),
                "mark_price": float(p.get("markPx") or 0),
                "unrealized_pnl_usd": float(p.get("upl") or 0),
                "leverage": int(float(p.get("lever") or 1)),
                "position_id": inst_id,
            })
        if not positions:
            return []
        import time as _t
        since_ms = int((_t.time() - 7 * 86400) * 1000)
        fundings = await asyncio.gather(*[
            cls._funding_pnl(creds, p["_inst_id"], since_ms) for p in positions
        ], return_exceptions=True)
        for p, f in zip(positions, fundings):
            p["funding_pnl_usd"] = f if isinstance(f, (int, float)) else None
            p.pop("_inst_id", None)
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
            if "50014" in msg or "Invalid" in msg:
                out["error"] = "Invalid API key"
            elif "50004" in msg or "permission" in msg.lower():
                out["error"] = "API key permissions insufficient"
            else:
                out["error"] = f"OKX rejected the key: {msg[:180]}"
            return out
        if need_trade:
            try:
                cfg = await cls._req(creds, "GET", "/api/v5/account/config")
                if cfg:
                    acct_lv = cfg[0].get("acctLv", "")
                    # acctLv 1=simple, 2=single-ccy margin, 3=multi-ccy, 4=portfolio
                    out["can_trade"] = acct_lv in ("1", "2", "3", "4")
                    if not out["can_trade"]:
                        out["error"] = "Account level does not support futures trading"
            except Exception as e:
                out["error"] = f"Trade-permission probe failed: {str(e)[:180]}"
        return out

    # ── Max leverage ──
    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        instruments = await _instruments()
        info = instruments.get(_okx_symbol(symbol))
        if info:
            return info.get("lever", 125)
        return 125
