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
logger = logging.getLogger("avalant.trade.htx")

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

    @classmethod
    async def set_leverage(
        cls, creds: dict, symbol: str, leverage: int, margin_mode: str,
    ) -> None:
        raise RuntimeError(
            "HTX futures trading not implemented in this adapter yet — spot only."
        )

    @classmethod
    async def place_order(
        cls, creds: dict, symbol: str, side: str,
        quantity: float, leverage: int = 1, margin_mode: str = "isolated",
    ) -> dict:
        acct = await cls._spot_account_id(creds)
        sym = cls._symbol(symbol)
        side_l = side.lower()
        if side_l not in ("buy", "sell"):
            raise ValueError(f"invalid side {side}")
        # MARKET orders on HTX: type = "<side>-market"; for market-buy the
        # `amount` field is interpreted as USDT spent, for market-sell as
        # base asset quantity. We expose quantity = base units, so
        # market-buy needs to be routed differently — force limit
        # execution at market for consistency.
        body = {
            "account-id": str(acct),
            "amount": f"{quantity:.8f}".rstrip("0").rstrip("."),
            "symbol": sym,
            "type": f"{side_l}-market",
            "source": "spot-api",
        }
        if side_l == "buy":
            raise RuntimeError(
                "HTX market-buy takes quote-asset (USDT) amount, not base quantity. "
                "Use a limit order for market-buy until this adapter adds quote-mode."
            )
        data = await cls._signed(creds, "POST", "/v1/order/orders/place", body=body)
        order_id = str(data.get("data") or "")
        return {"order_id": order_id, "avg_price": 0.0}  # fill-price requires /v1/order/orders/{id}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        raise RuntimeError(
            "HTX is spot-only in this adapter — use place_order(side='sell', ...) "
            "to unwind a spot position."
        )

    @classmethod
    async def list_positions(
        cls, creds: dict, symbol: str | None = None,
    ) -> list[dict]:
        return []
