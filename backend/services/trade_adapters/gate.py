"""Gate.io USDT Futures trade adapter (v4)."""
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

BASE = "https://api.gateio.ws"
logger = logging.getLogger("avalant.trade.gate")

_CONTRACT_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_CONTRACT_TTL = 600
_CONTRACT_LOCK = asyncio.Lock()


async def _contracts() -> dict[str, dict]:
    """Return {contract: {quanto_multiplier, order_size_min, leverage_min, leverage_max, mark_price_round}}."""
    now = time.time()
    if _CONTRACT_CACHE["data"] and now - _CONTRACT_CACHE["ts"] < _CONTRACT_TTL:
        return _CONTRACT_CACHE["data"]
    async with _CONTRACT_LOCK:
        if _CONTRACT_CACHE["data"] and time.time() - _CONTRACT_CACHE["ts"] < _CONTRACT_TTL:
            return _CONTRACT_CACHE["data"]
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{BASE}/api/v4/futures/usdt/contracts")
                items = r.json()
        except Exception as e:
            logger.warning("Gate contracts fetch failed: %s", e)
            return _CONTRACT_CACHE["data"] or {}
        out: dict[str, dict] = {}
        if not isinstance(items, list):
            return _CONTRACT_CACHE["data"] or {}
        for it in items:
            name = it.get("name", "")
            out[name] = {
                "quanto_multiplier": float(it.get("quanto_multiplier") or 1),
                "order_size_min": int(it.get("order_size_min") or 1),
                "order_size_max": int(it.get("order_size_max") or 1000000),
                "leverage_min": int(it.get("leverage_min") or 1),
                "leverage_max": int(it.get("leverage_max") or 100),
                "mark_price_round": float(it.get("mark_price_round") or 0.01),
                "order_price_round": float(it.get("order_price_round") or 0.01),
            }
        _CONTRACT_CACHE["data"] = out
        _CONTRACT_CACHE["ts"] = time.time()
        return out


_GATE_FRIENDLY = {
    "INVALID_KEY": "Invalid API key.",
    "INVALID_SIGNATURE": "Signature mismatch — API secret is wrong.",
    "INVALID_TIMESTAMP": "Clock skew — try again.",
    "KEY_EXPIRED": "API key has expired.",
    "FORBIDDEN": "API key does not have required permissions.",
    "BALANCE_NOT_ENOUGH": "Insufficient balance for margin.",
    "ORDER_SIZE_MIN": "Order size below minimum.",
    "ORDER_SIZE_MAX": "Order size exceeds maximum.",
    "CONTRACT_NOT_FOUND": "Contract not found on Gate.io Futures.",
    "RISK_LIMIT_EXCEEDED": "Risk limit exceeded.",
    "LEVERAGE_TOO_HIGH": "Leverage exceeds the exchange maximum.",
    "POSITION_NOT_FOUND": "No open position found.",
    "DUAL_SIDE_NOT_ALLOWED": "Dual-side mode not supported in current state.",
}


def _friendly_gate(label: str | None, msg: str) -> str:
    if label and label in _GATE_FRIENDLY:
        return _GATE_FRIENDLY[label]
    return msg or "Gate.io rejected the request."


def _split_label(exc: Exception) -> tuple[str | None, str]:
    import re
    s = str(exc)
    m = re.match(r"Gate (\S+): (.*)", s)
    if m:
        return m.group(1), m.group(2)
    return None, s


def _gate_symbol(s: str) -> str:
    return s.upper() + "_USDT"


class GateAdapter:
    @staticmethod
    def _sign(secret: str, method: str, path: str, query: str, body: str, ts: str) -> str:
        body_hash = hashlib.sha512(body.encode()).hexdigest()
        pre = f"{method}\n{path}\n{query}\n{body_hash}\n{ts}"
        return hmac.new(secret.encode(), pre.encode(), hashlib.sha512).hexdigest()

    @classmethod
    async def _req(cls, creds: dict, method: str, path: str,
                   query: dict | None = None, body: dict | None = None) -> Any:
        ts = str(int(time.time()))
        query_str = "&".join(f"{k}={query[k]}" for k in sorted(query)) if query else ""
        body_str = jsonlib.dumps(body, separators=(",", ":")) if body else ""
        sig = cls._sign(creds["api_secret"], method, path, query_str, body_str, ts)
        headers = {
            "KEY": creds["api_key"],
            "SIGN": sig,
            "Timestamp": ts,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = BASE + path
        if query_str:
            url += "?" + query_str
        async with httpx.AsyncClient(timeout=10) as c:
            if method == "GET":
                r = await c.get(url, headers=headers)
            elif method == "POST":
                r = await c.post(url, headers=headers, content=body_str)
            elif method == "DELETE":
                r = await c.delete(url, headers=headers)
            else:
                raise ValueError(method)
        if r.status_code >= 400:
            label = None
            msg = r.text
            try:
                j = r.json()
                label = j.get("label")
                msg = j.get("message") or j.get("detail") or r.text
            except Exception:
                pass
            raise RuntimeError(f"Gate {label or r.status_code}: {msg}")
        if r.status_code == 204 or not r.text.strip():
            return {}
        return r.json()

    # ── Balance ──
    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        data = await cls._req(creds, "GET", "/api/v4/futures/usdt/accounts")
        return {"usdt": float(data.get("available") or 0)}

    # ── Leverage ──
    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        contract = _gate_symbol(symbol)
        # Gate sets leverage per position; 0 = cross, >0 = isolated with that leverage
        lev_val = int(leverage) if margin_mode == "isolated" else 0
        try:
            await cls._req(creds, "POST",
                           f"/api/v4/futures/usdt/positions/{contract}/leverage",
                           body={"leverage": lev_val})
        except RuntimeError as e:
            s = str(e)
            # "leverage not changed" is fine
            if "not changed" not in s.lower() and "same" not in s.lower():
                raise RuntimeError(_friendly_gate(*_split_label(e)))
        # If isolated, also set the cross_leverage_limit (some Gate accounts need this)
        if margin_mode == "isolated":
            try:
                await cls._req(creds, "POST",
                               f"/api/v4/futures/usdt/positions/{contract}/leverage",
                               body={"leverage": int(leverage)})
            except RuntimeError:
                pass

    # ── Preflight ──
    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        contract = _gate_symbol(symbol)
        all_contracts = await _contracts()
        info = all_contracts.get(contract)
        if not info:
            return {"ok": False, "reason": f"{contract} is not listed on Gate.io Futures."}

        quanto = info["quanto_multiplier"]
        min_size = info["order_size_min"]

        # Convert coin quantity → contracts: 1 contract = quanto_multiplier coins
        if quanto > 0:
            num_contracts = int(quantity / quanto)
        else:
            num_contracts = int(quantity)

        if num_contracts < min_size:
            return {"ok": False,
                    "reason": f"Quantity below minimum ({min_size} contracts = {min_size * quanto} {symbol.upper()})."}

        if num_contracts > info["order_size_max"]:
            return {"ok": False, "reason": f"Order size exceeds maximum ({info['order_size_max']} contracts)."}

        # Mark price
        mark_price = 0.0
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{BASE}/api/v4/futures/usdt/contracts/{contract}")
                d = r.json()
                mark_price = float(d.get("mark_price") or d.get("last_price") or 0)
        except Exception:
            pass

        # Balance
        try:
            bal = (await cls.fetch_balance(creds)).get("usdt", 0)
        except RuntimeError as e:
            return {"ok": False, "reason": _friendly_gate(*_split_label(e))}

        notional = num_contracts * quanto * mark_price
        if mark_price and leverage > 0:
            required = notional / max(1, leverage)
            if bal + 0.01 < required:
                return {"ok": False,
                        "reason": f"Insufficient margin: need ~${required:.2f} USDT, have ${bal:.2f}."}

        max_lev = info.get("leverage_max", 100)
        if leverage > max_lev:
            return {"ok": False, "reason": f"Leverage {leverage}x exceeds max {max_lev}x for {contract}."}

        return {
            "ok": True,
            "qty_rounded": num_contracts * quanto,
            "contracts": num_contracts,
            "quanto_multiplier": quanto,
            "min_size": min_size,
        }

    # ── Place order ──
    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        contract = _gate_symbol(symbol)
        all_contracts = await _contracts()
        info = all_contracts.get(contract) or {}
        quanto = info.get("quanto_multiplier", 1)

        num_contracts = int(quantity / quanto) if quanto > 0 else int(quantity)
        if num_contracts <= 0:
            raise RuntimeError(f"Quantity too small for {contract}.")

        # Gate: positive size = long, negative = short
        size = num_contracts if side == "buy" else -num_contracts

        try:
            r = await cls._req(creds, "POST", "/api/v4/futures/usdt/orders", body={
                "contract": contract,
                "size": size,
                "price": "0",
                "tif": "ioc",
            })
        except RuntimeError as e:
            raise RuntimeError(_friendly_gate(*_split_label(e)))
        order_id = str(r.get("id", ""))
        fill_price = float(r.get("fill_price") or 0)
        return {"order_id": order_id, "avg_price": fill_price}

    # ── Close position ──
    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        contract = _gate_symbol(symbol)
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}

        p = positions[0]
        try:
            r = await cls._req(creds, "POST", "/api/v4/futures/usdt/orders", body={
                "contract": contract,
                "size": 0,
                "price": "0",
                "tif": "ioc",
                "close": True,
            })
        except RuntimeError as e:
            raise RuntimeError(_friendly_gate(*_split_label(e)))
        order_id = str(r.get("id", ""))
        return {"order_id": order_id, "closed_qty": p["quantity"], "realized_pnl_usd": p.get("unrealized_pnl_usd", 0)}

    # ── Positions ──
    @classmethod
    async def _funding_pnl(cls, creds: dict, contract: str, since_s: int) -> float | None:
        """Sum `change` from /api/v4/futures/usdt/account_book?type=fund."""
        try:
            data = await cls._req(creds, "GET",
                f"/api/v4/futures/usdt/account_book?type=fund&contract={contract}&from={since_s}&limit=100")
            return sum(float(x.get("change") or 0) for x in (data or []))
        except Exception:
            return None

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        data = await cls._req(creds, "GET", "/api/v4/futures/usdt/positions")
        if not isinstance(data, list):
            data = []
        all_contracts = await _contracts()
        positions = []
        for p in data:
            size = int(p.get("size") or 0)
            if size == 0:
                continue
            cname = p.get("contract", "")
            if symbol and cname != _gate_symbol(symbol):
                continue
            quanto = all_contracts.get(cname, {}).get("quanto_multiplier", 1)
            qty_coins = abs(size) * quanto
            sym = cname.replace("_USDT", "")
            positions.append({
                "exchange": "gate",
                "symbol": sym,
                "_contract": cname,
                "side": "buy" if size > 0 else "sell",
                "quantity": qty_coins,
                "entry_price": float(p.get("entry_price") or 0),
                "mark_price": float(p.get("mark_price") or 0),
                "unrealized_pnl_usd": float(p.get("unrealised_pnl") or 0),
                "leverage": int(float(p.get("leverage") or 1)),
                "position_id": cname,
            })
        if not positions:
            return []
        since_s = int(time.time() - 7 * 86400)
        fundings = await asyncio.gather(*[
            cls._funding_pnl(creds, p["_contract"], since_s) for p in positions
        ], return_exceptions=True)
        for p, f in zip(positions, fundings):
            p["funding_pnl_usd"] = f if isinstance(f, (int, float)) else None
            p.pop("_contract", None)
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
            if "INVALID_KEY" in msg:
                out["error"] = "Invalid API key"
            elif "INVALID_SIGNATURE" in msg:
                out["error"] = "Signature mismatch — API secret is wrong"
            elif "FORBIDDEN" in msg:
                out["error"] = "API key permissions insufficient"
            else:
                out["error"] = f"Gate.io rejected the key: {msg[:180]}"
            return out
        if need_trade:
            # Gate doesn't have a dedicated permissions endpoint;
            # try placing a tiny order that will fail on size to confirm trade access
            try:
                await cls._req(creds, "POST", "/api/v4/futures/usdt/orders", body={
                    "contract": "BTC_USDT",
                    "size": 0,
                    "price": "0",
                    "tif": "ioc",
                })
                out["can_trade"] = True
            except RuntimeError as e:
                msg = str(e)
                if "FORBIDDEN" in msg or "permission" in msg.lower():
                    out["error"] = "Key has no Futures trading permission"
                else:
                    # Any other error (like ORDER_SIZE_MIN) means the key CAN trade
                    out["can_trade"] = True
        return out

    # ── Max leverage ──
    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        all_contracts = await _contracts()
        info = all_contracts.get(_gate_symbol(symbol))
        if info:
            return info.get("leverage_max", 100)
        return 100
