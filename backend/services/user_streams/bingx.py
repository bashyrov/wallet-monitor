"""BingX swap user-stream.

BingX is loosely Binance-flavoured for the user-stream side:
  1. POST /openApi/user/auth/userDataStream  →  {listenKey}
  2. WS connect to wss://open-api-swap.bingx.com/swap-market?listenKey=<key>
  3. Receives ACCOUNT_UPDATE / ORDER_TRADE_UPDATE (Binance-clone)
  4. Renew listenKey via PUT every 30 min.

The events differ slightly in field names from Binance, so we parse
explicitly rather than reuse BinanceUserStream verbatim.

Reference:
  https://bingx-api.github.io/docs/#/swapV2/listenKey.html
"""
from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import time
from typing import Any

from backend.services.user_streams._base import (
    BaseUserStream, EVT_BALANCE_UPDATE, EVT_POSITION_UPDATE, UserStreamEvent,
)

logger = logging.getLogger("avalant.userstream.bingx")

REST_BASE = "https://open-api.bingx.com"
WS_BASE = "wss://open-api-swap.bingx.com/swap-market"
LISTEN_KEY_RENEW_INTERVAL_S = 30 * 60


class BingXUserStream(BaseUserStream):
    name = "bingx"

    @classmethod
    async def _post_listen_key(cls, creds: dict) -> str:
        from backend.providers.http import RetryClient
        async with RetryClient(timeout=10) as c:
            r = await c.post(
                f"{REST_BASE}/openApi/user/auth/userDataStream",
                headers={"X-BX-APIKEY": creds["api_key"]},
            )
            r.raise_for_status()
            data = r.json()
            return data["listenKey"]

    @classmethod
    async def _put_listen_key(cls, creds: dict, lk: str) -> None:
        from backend.providers.http import RetryClient
        async with RetryClient(timeout=10) as c:
            r = await c.put(
                f"{REST_BASE}/openApi/user/auth/userDataStream",
                params={"listenKey": lk},
                headers={"X-BX-APIKEY": creds["api_key"]},
            )
            if r.status_code >= 400:
                logger.warning("bingx listenKey PUT %s: %s", r.status_code, r.text[:200])

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        lk = await cls._post_listen_key(creds)
        creds["_listen_key"] = lk
        return f"{WS_BASE}?listenKey={lk}", {}

    @classmethod
    async def keep_alive_loop(cls, creds: dict, stop_event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(),
                                       timeout=LISTEN_KEY_RENEW_INTERVAL_S)
                return
            except asyncio.TimeoutError:
                pass
            lk = creds.get("_listen_key")
            if not lk:
                return
            try:
                await cls._put_listen_key(creds, lk)
            except Exception as exc:
                logger.warning("bingx: listenKey renew failed: %s", exc)

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        # BingX wraps events as gzip'd binary on the WS — but the
        # websockets library handles compression for permessage-deflate.
        # If we still get bytes here, decompress.
        if isinstance(raw, bytes):
            try:
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read().decode()
                raw = json.loads(raw)
            except Exception:
                return None

        if not isinstance(raw, dict):
            return None
        e = raw.get("e") or raw.get("eventType")

        # Heartbeat ping
        if isinstance(raw, dict) and raw.get("ping"):
            return None

        if e == "ACCOUNT_UPDATE":
            a = raw.get("a") or {}
            for b in (a.get("B") or []):
                if (b.get("a") or "").upper() == "USDT":
                    try:
                        bal = float(b.get("wb") or 0)
                    except (TypeError, ValueError):
                        bal = None
                    return UserStreamEvent(kind=EVT_BALANCE_UPDATE, balance_usdt=bal, raw=raw)
            for p in (a.get("P") or []):
                sym_full = (p.get("s") or "").upper()
                # BingX uses BTC-USDT format (with hyphen)
                if "-" in sym_full:
                    base = sym_full.split("-")[0]
                else:
                    if not sym_full.endswith("USDT"):
                        continue
                    base = sym_full[:-4]
                amt = float(p.get("pa") or 0)
                ep = p.get("ep")
                up = p.get("up")
                ps = (p.get("ps") or "").upper()
                if ps in ("LONG", "SHORT"):
                    side = "buy" if ps == "LONG" else "sell"
                else:
                    side = "buy" if amt > 0 else "sell"
                return UserStreamEvent(
                    kind=EVT_POSITION_UPDATE,
                    symbol=base,
                    side=side if amt != 0 else None,
                    qty=abs(amt),
                    entry_price=float(ep) if ep else None,
                    unrealized_pnl_usd=float(up) if up else None,
                    raw=raw,
                )
            return None

        return None
