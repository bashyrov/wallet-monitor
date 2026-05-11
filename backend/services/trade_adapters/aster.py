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

    # In-process nonce monotonic-by-microsecond + counter to avoid the
    # "100-most-recent-nonces, reject if smaller than minimum" rule.
    _NONCE_LOCK = None
    _NONCE_LAST = 0
    _NONCE_COUNTER = 0

    @classmethod
    def _next_nonce(cls) -> int:
        import threading
        if cls._NONCE_LOCK is None:
            cls._NONCE_LOCK = threading.Lock()
        with cls._NONCE_LOCK:
            now = int(time.time())
            if now == cls._NONCE_LAST:
                cls._NONCE_COUNTER += 1
            else:
                cls._NONCE_LAST = now
                cls._NONCE_COUNTER = 0
            return now * 1_000_000 + cls._NONCE_COUNTER

    @classmethod
    async def _signed(cls, creds: dict, method: str, path: str, params: dict | None = None,
                      host: str | None = None) -> Any:
        """Aster V3 Pro API EIP-712 signing (chainId 1666, primaryType=Message).
        Replaces the legacy V1 X-AB-APIKEY + HMAC scheme — V1 key creation
        was closed on 2026-03-25 so new credentials only work via V3.

        creds layout (we store both):
          api_key    → master/login wallet address ("user" param)
          api_secret → API wallet private key ("signer" derived from it)

        host override (e.g. sapi.asterdex.com) lets the same signer hit
        Aster Spot V3 endpoints (same EIP-712 scheme, different base URL).
        """
        try:
            from eth_account import Account
            import urllib.parse
            # eth_account renamed encode_structured_data → encode_typed_data
            # somewhere in the 0.10 line. Support both.
            try:
                from eth_account.messages import encode_typed_data as _encode
            except ImportError:
                from eth_account.messages import encode_structured_data as _encode
        except ImportError:
            raise RuntimeError("eth_account package required for Aster V3")

        master = (creds.get("api_key") or "").strip()
        priv = (creds.get("api_secret") or "").strip()
        if not master or not priv:
            raise RuntimeError("Aster requires master wallet address + API wallet private key")
        acct = Account.from_key(priv if priv.startswith("0x") else "0x" + priv)
        signer_addr = acct.address

        # Field order matters — must match the prior working implementation
        # (commit 8e46c44, perp_dexes/aster_provider.py). asterChain is in
        # the demo but breaks agent lookup on prod: nonce → user → signer.
        body = dict(params or {})
        body["nonce"] = str(cls._next_nonce())
        body["user"] = master
        body["signer"] = signer_addr
        msg = urllib.parse.urlencode(body)

        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Message": [
                    {"name": "msg", "type": "string"},
                ],
            },
            "primaryType": "Message",
            "domain": {
                "name": "AsterSignTransaction",
                "version": "1",
                "chainId": 1666,
                "verifyingContract": "0x0000000000000000000000000000000000000000",
            },
            "message": {"msg": msg},
        }
        # encode_typed_data() in newer eth_account interprets positional
        # arg as the domain only. We MUST pass via full_message=… kwarg.
        # Fall back to positional for older versions where that's correct.
        try:
            em = _encode(full_message=typed_data)
        except TypeError:
            em = _encode(typed_data)
        signed = Account.sign_message(em, private_key=priv if priv.startswith("0x") else "0x" + priv)
        sig_hex = signed.signature.hex()

        from backend.services.trade_adapters._http import http_client
        client = http_client(host or BASE, timeout=10.0)
        url = f"{path}?{msg}&signature={sig_hex}"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "avalant-fetcher/1.0",
        }
        if method == "GET":
            r = await client.get(url, headers=headers)
        elif method == "POST":
            r = await client.post(url, headers=headers)
        elif method == "DELETE":
            r = await client.delete(url, headers=headers)
        else:
            raise ValueError(method)
        if r.status_code >= 400:
            msg_err = r.text[:240]
            try:
                j = r.json()
                msg_err = str(j.get("msg") or j.get("message") or msg_err)
            except Exception:
                pass
            raise RuntimeError(f"Aster {r.status_code}: {msg_err}")
        return r.json()

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        """Aster has separate futures + spot accounts (same API wallet keys,
        different base URL). Futures lives on fapi.asterdex.com, spot on
        sapi.asterdex.com. Both use the same V3 EIP-712 signing."""
        STABLES = ("USDT", "USDC", "USD1", "BUSD")

        fut_usd = 0.0
        try:
            data = await cls._signed(creds, "GET", "/fapi/v3/balance")
            for x in (data if isinstance(data, list) else []):
                if (x.get("asset") or "").upper() in STABLES:
                    try:
                        fut_usd += float(x.get("availableBalance") or 0)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass

        spot_usd = 0.0
        try:
            # Note: requires the API agent to have `canSpotTrade=True` (set
            # via /fapi/v3/approveAgent or /fapi/v3/updateAgent). Agents
            # created perp-only return 500 here — we silently skip and the
            # futures balance is still reported correctly.
            data = await cls._signed(creds, "GET", "/api/v3/account",
                                     host="https://sapi.asterdex.com")
            for b in (data or {}).get("balances", []):
                if (b.get("asset") or "").upper() in STABLES:
                    try:
                        spot_usd += float(b.get("free") or 0) + float(b.get("locked") or 0)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass

        return {"usdt": fut_usd + spot_usd, "spot_usd": spot_usd, "futures_usd": fut_usd}

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
