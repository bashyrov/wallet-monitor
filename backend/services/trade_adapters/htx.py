"""HTX (Huobi) trade adapter.

Current scope: **spot-only**. HTX's USDT-M linear swap (hbdm.com) is
stubbed out in the adapter — set_leverage / place_order on futures will
raise with a clear message so the UI doesn't falsely claim support.
Adding futures is a dedicated sprint (order endpoints differ, leverage
per-contract, cross vs isolated state).

Signing: HMAC-SHA256 over
    "GET\napi.huobi.pro\n/path\n<sorted, url-encoded query>"
result is base64-encoded. Timestamp format:
`AccessKeyId`, `SignatureMethod=HmacSHA256`, `SignatureVersion=2`,
`Timestamp=YYYY-MM-DDTHH%3AMM%3ASS` are included in the query.

Protocol notes:
  · fetch_balance  — spot USDT balance (sum of trade+frozen).
  · place_order    — MARKET order via /v1/order/orders/place.
  · set_leverage / close_position / list_positions — spot has no positions;
    returns {} / [] or raises informatively.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from backend.providers.exchanges._signing import b64_hmac_sha256

SPOT_BASE = "https://api.huobi.pro"
SPOT_HOST = "api.huobi.pro"
FUT_BASE = "https://api.hbdm.com"
FUT_HOST = "api.hbdm.com"
logger = logging.getLogger("avalant.trade.htx")

# Futures contract metadata cache (contract_size per symbol)
_FUT_INSTR: dict[str, dict] = {}
_FUT_INSTR_TS: float = 0.0
_FUT_INSTR_TTL = 1800.0
_FUT_INSTR_LOCK = asyncio.Lock()


async def _fut_contracts() -> dict[str, dict]:
    """{contract_code: {contract_size, price_tick, status}} for HTX linear swap."""
    global _FUT_INSTR_TS
    if _FUT_INSTR and (time.time() - _FUT_INSTR_TS) < _FUT_INSTR_TTL:
        return _FUT_INSTR
    async with _FUT_INSTR_LOCK:
        if _FUT_INSTR and (time.time() - _FUT_INSTR_TS) < _FUT_INSTR_TTL:
            return _FUT_INSTR
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(f"{FUT_BASE}/linear-swap-api/v1/swap_contract_info")
                items = (r.json() or {}).get("data") or []
        except Exception:
            return _FUT_INSTR
        out = {}
        for it in items:
            code = it.get("contract_code") or ""
            if not code or it.get("contract_status") != 1:
                continue
            out[code] = {
                "contract_size": float(it.get("contract_size") or 0),
                "price_tick": float(it.get("price_tick") or 0),
            }
        if out:
            _FUT_INSTR.clear()
            _FUT_INSTR.update(out)
            _FUT_INSTR_TS = time.time()
        return _FUT_INSTR

# {api_key: (account_id, ts)} — HTX account ids are stable, cache forever.
_ACCT_CACHE: dict[str, int] = {}
_ACCT_LOCK = asyncio.Lock()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _sign_payload(method: str, host: str, path: str, params: dict) -> str:
    items = sorted(params.items(), key=lambda kv: kv[0])
    qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in items)
    return f"{method.upper()}\n{host}\n{path}\n{qs}"


class HtxAdapter:
    @staticmethod
    def _symbol(s: str) -> str:
        return (s + "usdt").lower()

    @classmethod
    async def _signed(
        cls, creds: dict, method: str, path: str,
        params: dict | None = None, body: dict | None = None,
    ) -> Any:
        p: dict[str, Any] = dict(params or {})
        p["AccessKeyId"] = creds["api_key"]
        p["SignatureMethod"] = "HmacSHA256"
        p["SignatureVersion"] = "2"
        p["Timestamp"] = _ts()
        payload = _sign_payload(method, SPOT_HOST, path, p)
        p["Signature"] = b64_hmac_sha256(creds["api_secret"], payload)
        qs = urlencode(p, quote_via=quote)
        url = f"{SPOT_BASE}{path}?{qs}"
        async with httpx.AsyncClient(timeout=10) as c:
            if method == "GET":
                r = await c.get(url)
            elif method == "POST":
                import json as _j
                r = await c.post(
                    url,
                    content=_j.dumps(body or {}, separators=(",", ":")),
                    headers={"Content-Type": "application/json"},
                )
            else:
                raise ValueError(method)
        if r.status_code >= 400:
            raise RuntimeError(f"HTX {r.status_code}: {r.text}")
        data = r.json()
        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError(f"HTX API: {data.get('err-msg') or data}")
        return data

    @classmethod
    async def _spot_account_id(cls, creds: dict) -> int:
        key = creds["api_key"]
        if key in _ACCT_CACHE:
            return _ACCT_CACHE[key]
        async with _ACCT_LOCK:
            if key in _ACCT_CACHE:
                return _ACCT_CACHE[key]
            data = await cls._signed(creds, "GET", "/v1/account/accounts")
            for a in (data.get("data") or []):
                if a.get("type") == "spot" and a.get("state") == "working":
                    acct_id = int(a["id"])
                    _ACCT_CACHE[key] = acct_id
                    return acct_id
            raise RuntimeError("HTX: no active spot account on this key")

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        acct = await cls._spot_account_id(creds)
        data = await cls._signed(creds, "GET", f"/v1/account/accounts/{acct}/balance")
        free_usdt = 0.0
        for row in (data.get("data") or {}).get("list") or []:
            if (row.get("currency") or "").upper() == "USDT" and row.get("type") == "trade":
                free_usdt += float(row.get("balance") or 0)
        return {"usdt": free_usdt}

    # ── Futures-side signing (api.hbdm.com) ──────────────────────────────────
    @classmethod
    async def _signed_fut(
        cls, creds: dict, method: str, path: str,
        params: dict | None = None, body: dict | None = None,
    ) -> Any:
        import json as _j
        p: dict[str, Any] = dict(params or {})
        p["AccessKeyId"] = creds["api_key"]
        p["SignatureMethod"] = "HmacSHA256"
        p["SignatureVersion"] = "2"
        p["Timestamp"] = _ts()
        payload = _sign_payload(method, FUT_HOST, path, p)
        p["Signature"] = b64_hmac_sha256(creds["api_secret"], payload)
        qs = urlencode(p, quote_via=quote)
        url = f"{FUT_BASE}{path}?{qs}"
        async with httpx.AsyncClient(timeout=10) as c:
            if method == "POST":
                r = await c.post(
                    url,
                    content=_j.dumps(body or {}, separators=(",", ":")),
                    headers={"Content-Type": "application/json"},
                )
            else:
                r = await c.get(url)
        if r.status_code >= 400:
            raise RuntimeError(f"HTX-fut {r.status_code}: {r.text[:200]}")
        data = r.json()
        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError(f"HTX-fut: {data.get('err_msg') or data.get('err-msg') or data}")
        return data

    @classmethod
    async def set_leverage(
        cls, creds: dict, symbol: str, leverage: int, margin_mode: str,
    ) -> None:
        contract_code = f"{symbol.upper()}-USDT"
        try:
            await cls._signed_fut(creds, "POST",
                                   "/linear-swap-api/v1/swap_cross_switch_lever_rate",
                                   body={
                                       "contract_code": contract_code,
                                       "lever_rate": int(max(1, leverage)),
                                   })
        except Exception as e:
            logger.info("htx set_leverage(%s, %sx) note: %s", contract_code, leverage, e)

    @classmethod
    async def place_order(
        cls, creds: dict, symbol: str, side: str,
        quantity: float, leverage: int = 1, margin_mode: str = "isolated",
    ) -> dict:
        contract_code = f"{symbol.upper()}-USDT"
        contracts = await _fut_contracts()
        info = contracts.get(contract_code)
        if not info:
            raise RuntimeError(f"HTX-fut: no active contract for {contract_code}")
        contract_size = info["contract_size"]
        if contract_size <= 0:
            raise RuntimeError(f"HTX-fut: invalid contract_size for {contract_code}")
        # Quantity comes in as base units; HTX wants integer "volume" of contracts.
        volume = int(round(float(quantity) / contract_size))
        if volume <= 0:
            raise RuntimeError(
                f"HTX-fut: qty {quantity} below 1 contract ({contract_size} {symbol})"
            )
        is_buy = (side or "").lower() in ("buy", "long")
        body = {
            "contract_code": contract_code,
            "volume": volume,
            "direction": "buy" if is_buy else "sell",
            "offset": "open",
            "lever_rate": int(max(1, leverage)),
            "order_price_type": "optimal_20",  # market-equivalent: take best-of-20 levels
        }
        data = await cls._signed_fut(creds, "POST",
                                      "/linear-swap-api/v1/swap_cross_order",
                                      body=body)
        d = data.get("data") or {}
        return {"order_id": str(d.get("order_id_str") or d.get("order_id") or ""),
                "avg_price": 0.0}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        contract_code = f"{symbol.upper()}-USDT"
        positions = await cls.list_positions(creds, symbol=symbol)
        match = next((p for p in positions if (p.get("symbol") or "").upper() == symbol.upper()), None)
        if not match:
            return {"order_id": "", "closed_qty": 0.0, "realized_pnl_usd": 0.0}
        qty = abs(float(match.get("quantity") or 0))
        if qty <= 0:
            return {"order_id": "", "closed_qty": 0.0, "realized_pnl_usd": 0.0}
        contracts = await _fut_contracts()
        info = contracts.get(contract_code) or {"contract_size": 0}
        cs = info["contract_size"]
        if cs <= 0:
            return {"order_id": "", "closed_qty": 0.0, "realized_pnl_usd": 0.0}
        volume = int(round(qty / cs))
        opposite = "sell" if (match.get("side") or "").lower() == "buy" else "buy"
        body = {
            "contract_code": contract_code,
            "volume": volume,
            "direction": opposite,
            "offset": "close",
            "lever_rate": match.get("leverage") or 1,
            "order_price_type": "optimal_20",
        }
        data = await cls._signed_fut(creds, "POST",
                                      "/linear-swap-api/v1/swap_cross_order",
                                      body=body)
        d = data.get("data") or {}
        return {
            "order_id": str(d.get("order_id_str") or d.get("order_id") or ""),
            "closed_qty": qty,
            "realized_pnl_usd": float(match.get("unrealized_pnl_usd") or 0),
        }

    @classmethod
    async def list_positions(
        cls, creds: dict, symbol: str | None = None,
    ) -> list[dict]:
        try:
            data = await cls._signed_fut(creds, "POST",
                                          "/linear-swap-api/v1/swap_cross_position_info",
                                          body={})
        except Exception as e:
            logger.debug("htx list_positions: %s", e)
            return []
        items = data.get("data") or []
        out: list[dict] = []
        contracts = await _fut_contracts()
        for p in items:
            cc = p.get("contract_code") or ""
            base = cc.replace("-USDT", "").upper()
            if symbol and base != symbol.upper():
                continue
            cs = (contracts.get(cc) or {}).get("contract_size") or 1.0
            try:
                volume = float(p.get("volume") or 0)
            except (TypeError, ValueError):
                volume = 0.0
            if volume == 0:
                continue
            qty_base = volume * cs
            side = "buy" if (p.get("direction") or "").lower() == "buy" else "sell"
            out.append({
                "exchange": "htx",
                "symbol": base,
                "side": side,
                "quantity": qty_base,
                "entry_price": float(p.get("cost_open") or p.get("cost_hold") or 0),
                "unrealized_pnl_usd": float(p.get("profit_unreal") or 0),
                "leverage": int(float(p.get("lever_rate") or 0)) or None,
                "margin_mode": "cross",
            })
        return out
