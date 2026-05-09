"""Aster DEX trade adapter — EIP-712 API wallet signing.

Users create an API wallet at asterdex.com/en/api-wallet.
  creds = {"api_key": "0x... (main wallet)", "api_secret": "0x... (API signer private key)"}

Signing: EIP-712 typed data, Binance-like FAPI endpoint structure.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from typing import Any

import httpx

logger = logging.getLogger("avalant.trade.aster")

BASE = "https://fapi.asterdex.com"

_INSTR_CACHE: dict[str, tuple[dict, float]] = {}
_INSTR_TTL = 600
_INSTR_LOCK = asyncio.Lock()


class AsterAdapter:

    @staticmethod
    def _symbol(s: str) -> str:
        return s.upper() + "USDT"

    @classmethod
    async def _signed(cls, creds: dict, method: str, path: str, params: dict | None = None) -> Any:
        """EIP-712 signing — same as Aster's API wallet auth."""
        try:
            from eth_account import Account
            from eth_account.messages import encode_typed_data
        except ImportError:
            raise RuntimeError("eth_account package required for Aster trading")

        params = dict(params or {})
        params["timestamp"] = str(int(time.time() * 1e6))  # microseconds
        params["recvWindow"] = "5000"

        # Build query string
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))

        # EIP-712 typed data
        domain = {
            "name": "AsterSignTransaction",
            "chainId": 1666,
        }
        types = {
            "AsterSignTransaction": [
                {"name": "params", "type": "string"},
            ],
        }
        message = {"params": qs}

        acct = Account.from_key(creds["api_secret"])
        full_message = {
            "types": types,
            "primaryType": "AsterSignTransaction",
            "domain": domain,
            "message": message,
        }
        signed = acct.sign_typed_data(full_message=full_message)
        sig_hex = signed.signature.hex()
        if not sig_hex.startswith("0x"):
            sig_hex = "0x" + sig_hex

        headers = {
            "X-AB-APIKEY": creds["api_key"],
        }

        # Persistent client per host — eliminates TLS handshake on each call.
        from backend.services.trade_adapters._http import http_client
        client = http_client(BASE, timeout=10.0)
        rel = f"{path}?{qs}&signature={sig_hex}"
        if method == "GET":
            r = await client.get(rel, headers=headers)
        elif method == "POST":
            r = await client.post(rel, headers=headers)
        elif method == "DELETE":
            r = await client.delete(rel, headers=headers)
        else:
            raise ValueError(method)
        if r.status_code >= 400:
            msg = r.text[:200]
            try:
                j = r.json()
                msg = str(j.get("msg", msg))
            except Exception:
                pass
            raise RuntimeError(f"Aster {r.status_code}: {msg}")
        return r.json()

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        data = await cls._signed(creds, "GET", "/fapi/v2/balance")
        for x in (data if isinstance(data, list) else []):
            if x.get("asset") == "USDT":
                return {"usdt": float(x.get("availableBalance", 0) or 0)}
        return {"usdt": 0.0}

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["can_trade"] = True  # if balance works, API wallet has trade perms
            out["balance_usdt"] = bal.get("usdt", 0)
        except Exception as e:
            out["error"] = f"Aster: {str(e)[:180]}"
        return out

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        sym = cls._symbol(symbol)

        async def _mode():
            try:
                await cls._signed(creds, "POST", "/fapi/v1/marginType",
                                  {"symbol": sym, "marginType": "ISOLATED" if margin_mode == "isolated" else "CROSSED"})
            except RuntimeError as e:
                if "No need" not in str(e) and "-4046" not in str(e):
                    raise

        async def _lev():
            await cls._signed(creds, "POST", "/fapi/v1/leverage",
                              {"symbol": sym, "leverage": str(leverage)})

        await asyncio.gather(_mode(), _lev())

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        sym = cls._symbol(symbol)
        r = await cls._signed(creds, "POST", "/fapi/v1/order", {
            "symbol": sym,
            "side": "BUY" if side == "buy" else "SELL",
            "type": "MARKET",
            "quantity": f"{quantity:.6f}".rstrip("0").rstrip("."),
        })
        return {"order_id": str(r.get("orderId", "")), "avg_price": float(r.get("avgPrice", 0) or 0)}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        """Reduce-only market order. Previous implementation just called
        place_order(close_side, qty) which, without reduceOnly, would OPEN a
        new opposing position in hedge mode and only work by accident in
        one-way mode when position net goes to zero. Now mirrors the binance
        adapter — submit a MARKET order with reduceOnly=true."""
        positions = await cls.list_positions(creds, symbol)
        if not positions:
            return {"order_id": None, "closed_qty": 0, "realized_pnl_usd": 0}
        p = positions[0]
        sym = cls._symbol(symbol)
        reduce_side = "SELL" if p["side"] == "buy" else "BUY"
        qty_s = f"{p['quantity']:.6f}".rstrip("0").rstrip(".")
        try:
            r = await cls._signed(creds, "POST", "/fapi/v1/order", {
                "symbol": sym,
                "side": reduce_side,
                "type": "MARKET",
                "quantity": qty_s,
                "reduceOnly": "true",
            })
        except RuntimeError as e:
            raise RuntimeError(_friendly_error(*_split_code(e)) if '_friendly_error' in globals() else str(e))
        return {
            "order_id": str(r.get("orderId", "")),
            "closed_qty": p["quantity"],
            "realized_pnl_usd": p.get("unrealized_pnl_usd", 0),
        }

    # Per-user funding cache — bulk fetch (no symbol filter) once per 30s
    # then bucket by symbol in memory. Same shape as the Binance adapter.
    _FUNDING_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
    _FUNDING_CACHE_TTL_S = 30.0

    @classmethod
    async def _funding_pnl_bulk(cls, creds: dict, since_ms: int) -> dict[str, float]:
        """Aster is a Binance fork — single /fapi/v1/income call returns
        all FUNDING_FEE events for the account. Bucketed per-symbol in
        memory so list_positions can dispatch with zero extra API calls."""
        import time as _t
        api_key = (creds.get("api_key") or "").strip()
        cached = cls._FUNDING_CACHE.get(api_key)
        if cached and (_t.time() - cached[0]) < cls._FUNDING_CACHE_TTL_S:
            return cached[1]
        try:
            data = await cls._signed(creds, "GET", "/fapi/v1/income", {
                "incomeType": "FUNDING_FEE",
                "startTime": since_ms,
                "limit": 1000,
            })
        except Exception:
            return {}
        out: dict[str, float] = {}
        for ev in (data or []):
            sym = (ev.get("symbol") or "").upper()
            try:
                out[sym] = out.get(sym, 0.0) + float(ev.get("income") or 0)
            except (TypeError, ValueError):
                continue
        cls._FUNDING_CACHE[api_key] = (_t.time(), out)
        return out

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        params = {"symbol": cls._symbol(symbol)} if symbol else {}
        data = await cls._signed(creds, "GET", "/fapi/v2/positionRisk", params or None)
        positions = []
        for p in (data if isinstance(data, list) else []):
            amt = float(p.get("positionAmt", 0) or 0)
            if amt == 0:
                continue
            positions.append({
                "exchange": "aster",
                "symbol": str(p.get("symbol", "")).replace("USDT", ""),
                "_api_symbol": str(p.get("symbol", "")),
                "side": "buy" if amt > 0 else "sell",
                "quantity": abs(amt),
                "entry_price": float(p.get("entryPrice", 0) or 0),
                "mark_price": float(p.get("markPrice", 0) or 0),
                "unrealized_pnl_usd": float(p.get("unRealizedProfit", 0) or 0),
                "leverage": int(float(p.get("leverage", 1) or 1)),
                "position_id": str(p.get("symbol", "")),
            })
        if not positions:
            return []
        import time as _t
        since_ms = int((_t.time() - 7 * 86400) * 1000)
        funding_by_sym = await cls._funding_pnl_bulk(creds, since_ms)
        for p in positions:
            api_sym = p.pop("_api_symbol", "")
            v = funding_by_sym.get((api_sym or "").upper())
            p["funding_pnl_usd"] = v if v is not None else None
        return positions

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        return 100

    # Lazy-cached exchangeInfo. Aster is Binance-clone — same payload
    # shape with LOT_SIZE / MIN_NOTIONAL filters per symbol.
    _ex_info: dict[str, dict] = {}
    _ex_info_at: float = 0.0
    _EX_INFO_TTL = 600.0

    @classmethod
    async def _fetch_ex_info(cls) -> dict[str, dict]:
        import time as _t, httpx as _h
        if cls._ex_info and (_t.time() - cls._ex_info_at) < cls._EX_INFO_TTL:
            return cls._ex_info
        async with _h.AsyncClient(timeout=8) as c:
            r = await c.get(BASE + "/fapi/v1/exchangeInfo")
            r.raise_for_status()
            d = r.json() or {}
        out = {}
        for s in d.get("symbols", []):
            sym = s.get("symbol")
            if not sym:
                continue
            info = {
                "stepSize": None, "minQty": None, "minNotional": None,
                "tickSize": None,
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
        cls._ex_info = out
        cls._ex_info_at = _t.time()
        return out

    @classmethod
    async def get_public_qty_limits(cls, symbol: str) -> dict | None:
        try:
            info = (await cls._fetch_ex_info()).get(symbol.upper() + "USDT")
        except Exception:
            return None
        if not info:
            return None
        return {
            "min_qty": float(info.get("minQty") or 0),
            "step":    float(info.get("stepSize") or 0) or None,
            "min_notional": float(info.get("minNotional") or 0) or None,
            "max_qty": None,
            "unit": "coin",
        }

    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        # Use the public exchangeInfo we cache for the qty hint to enforce
        # min/step/min-notional at preflight time too — was no-op before,
        # so sub-min orders would slip through and fail mid-flight.
        try:
            info = (await cls._fetch_ex_info()).get(symbol.upper() + "USDT")
        except Exception:
            info = None
        if not info:
            return {"ok": True, "qty_rounded": quantity}
        step = float(info.get("stepSize") or 0)
        min_qty = float(info.get("minQty") or 0)
        min_not = float(info.get("minNotional") or 0)
        qty_r = quantity
        if step > 0:
            import math as _m
            qty_r = _m.floor(quantity / step) * step
        if qty_r <= 0 or qty_r < min_qty:
            return {"ok": False, "reason": f"Aster min qty is {min_qty} {symbol.upper()}."}
        return {"ok": True, "qty_rounded": qty_r, "step_size": step, "min_qty": min_qty, "min_notional": min_not}

    @classmethod
    async def fetch_recent_fills(cls, creds: dict, since_ts, *,
                                 market: str = "futures") -> list[dict]:
        """Aster is a Binance-API fork — same /fapi/v1/userTrades + income.
        Spot returns []."""
        from datetime import datetime as _dt
        if market != "futures":
            return []
        start_ms = int(since_ts.timestamp() * 1000)
        out: list[dict] = []
        try:
            income = await cls._signed(creds, "GET", "/fapi/v1/income", {
                "startTime": start_ms, "limit": 1000,
            }) or []
        except Exception:
            income = []
        symbols: set[str] = set()
        for it in income:
            try:
                sym = str(it.get("symbol") or "")
                if sym:
                    symbols.add(sym)
                if str(it.get("incomeType") or "") == "FUNDING_FEE":
                    ts_ms = int(it.get("time") or 0)
                    if ts_ms <= 0:
                        continue
                    out.append({
                        "symbol": sym.replace("USDT", ""),
                        "side": None, "qty": 0.0, "price": 0.0, "fee_usd": None,
                        "realized_pnl_usd": float(it.get("income") or 0),
                        "ts": _dt.utcfromtimestamp(ts_ms / 1000),
                        "ext_trade_id": str(it.get("tranId")
                                            or f"funding-{ts_ms}-{sym}"),
                        "ext_order_id": None,
                        "kind": "funding",
                    })
            except Exception:
                continue
        for sym in symbols:
            try:
                rows = await cls._signed(creds, "GET", "/fapi/v1/userTrades", {
                    "symbol": sym, "startTime": start_ms, "limit": 1000,
                }) or []
            except Exception:
                continue
            for r in rows:
                try:
                    ts_ms = int(r.get("time") or 0)
                    if ts_ms <= 0:
                        continue
                    side = "buy" if str(r.get("side") or "").upper() == "BUY" else "sell"
                    qty = float(r.get("qty") or 0)
                    if qty <= 0:
                        continue
                    rpnl = r.get("realizedPnl")
                    out.append({
                        "symbol": sym.replace("USDT", ""),
                        "side": side, "qty": qty,
                        "price": float(r.get("price") or 0),
                        "fee_usd": float(r.get("commission") or 0),
                        "realized_pnl_usd": (float(rpnl) if rpnl not in (None, "") else None),
                        "ts": _dt.utcfromtimestamp(ts_ms / 1000),
                        "ext_trade_id": str(r.get("id") or ""),
                        "ext_order_id": str(r.get("orderId") or "") or None,
                        "kind": "trade",
                    })
                except Exception:
                    continue
        return out
