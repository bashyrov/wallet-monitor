"""WhiteBIT collateral-futures user-stream.

Flow:
  1. Mint a single-use WS auth token via POST /api/v4/profile/websocket_token
     (HMAC-SHA512 signed body, same pattern as the trade adapter).
  2. Connect wss://api.whitebit.com/ws
  3. Send {"id":1,"method":"authorize","params":[<token>,"public"]}
  4. After authorize OK, subscribe to:
       balanceCol_subscribe   — collateral balance updates
       dealsCol_subscribe     — executed deals (proxy for position changes,
                                since WhiteBIT doesn't push positions directly)

Private WS docs:
  https://github.com/whitebit-exchange/api-docs/blob/main/pages/ws/private/colateral.md

Position state isn't pushed natively — the supervisor's REST seed populates
it on connect, then deal events drive the rebuild. This is the same shape
used by venues that lack a positions-only channel (HTX et al).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from backend.services.user_streams._base import (
    BaseUserStream, EVT_BALANCE_UPDATE, EVT_POSITION_UPDATE, UserStreamEvent,
)

logger = logging.getLogger("avalant.userstream.whitebit")

WS_URL = "wss://api.whitebit.com/ws"
REST_BASE = "https://whitebit.com"


def _sign(body_json: str, secret: str) -> tuple[str, str]:
    payload_b64 = base64.b64encode(body_json.encode()).decode()
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha512).hexdigest()
    return payload_b64, sig


async def _ws_token(creds: dict) -> str:
    api_key = (creds.get("api_key") or "").strip()
    api_secret = (creds.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("whitebit user-stream: missing api_key/api_secret")
    body = {
        "request": "/api/v4/profile/websocket_token",
        "nonce": int(time.time() * 1000),
    }
    body_json = json.dumps(body, separators=(",", ":"))
    payload_b64, sig = _sign(body_json, api_secret)
    headers = {
        "X-TXC-APIKEY": api_key,
        "X-TXC-PAYLOAD": payload_b64,
        "X-TXC-SIGNATURE": sig,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(REST_BASE + "/api/v4/profile/websocket_token",
                         content=body_json, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"whitebit ws_token: {r.status_code} {r.text[:200]}")
        data = r.json() or {}
        token = data.get("websocket_token") or data.get("token")
        if not token:
            raise RuntimeError(f"whitebit ws_token: empty response {data}")
        return str(token)


class WhitebitUserStream(BaseUserStream):
    name = "whitebit"

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        return WS_URL, {}

    @classmethod
    async def subscribe(cls, ws, creds: dict) -> None:
        token = await _ws_token(creds)
        await ws.send(json.dumps({
            "id": 1,
            "method": "authorize",
            "params": [token, "public"],
        }))
        # Wait for authorize response so we don't subscribe before auth lands.
        # WhiteBIT replies on the same id; at most a few frames precede.
        import asyncio as _aio
        for _ in range(5):
            try:
                raw = await _aio.wait_for(ws.recv(), timeout=5.0)
            except _aio.TimeoutError:
                raise RuntimeError("whitebit authorize: no response")
            try:
                msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except Exception:
                continue
            if msg.get("id") == 1:
                if msg.get("error"):
                    raise RuntimeError(f"whitebit authorize: {msg['error']}")
                break
        else:
            raise RuntimeError("whitebit authorize: no matching ack")

        # Subscribe to collateral balance + deals — WhiteBIT doesn't expose
        # a positions stream, so deal pushes drive the rebuild.
        await ws.send(json.dumps({
            "id": 2, "method": "balanceCol_subscribe", "params": [],
        }))
        await ws.send(json.dumps({
            "id": 3, "method": "dealsCol_subscribe", "params": [],
        }))

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        if not isinstance(raw, dict):
            return None
        method = raw.get("method") or ""
        # balanceCol_update: params=[market, {available,freeze,...}] for one currency
        if method == "balanceCol_update":
            params = raw.get("params") or []
            if len(params) >= 2 and isinstance(params[1], dict):
                ccy = (params[0] or "").upper() if isinstance(params[0], str) else "USDT"
                if ccy in ("USDT", "USDC"):
                    info = params[1] or {}
                    bal = info.get("available") or info.get("balance")
                    try:
                        bal_f = float(bal) if bal is not None else None
                    except (TypeError, ValueError):
                        bal_f = None
                    return UserStreamEvent(kind=EVT_BALANCE_UPDATE, balance_usdt=bal_f, raw=raw)
        # dealsCol_update: params=[id, market, ...] — extract symbol so the
        # supervisor flushes the snapshot for that key. Position state itself
        # is reseeded via REST on the next ensures_running tick.
        if method == "dealsCol_update":
            params = raw.get("params") or []
            if len(params) >= 2 and isinstance(params[1], str):
                market = params[1]
                if market.endswith("_PERP"):
                    base = market[:-len("_PERP")]
                    return UserStreamEvent(
                        kind=EVT_POSITION_UPDATE,
                        symbol=base,
                        qty=0.0,  # 0 here forces supervisor to re-seed via REST
                        raw=raw,
                    )
        return None
