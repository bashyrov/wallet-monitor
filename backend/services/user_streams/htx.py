"""HTX (Huobi) spot user-stream — auth on connect via signed JSON.

HTX exposes two private WS surfaces; we use the spot v2 feed since the
trade adapter is currently spot-only:
  wss://api.huobi.pro/ws/v2 — accounts.update, orders, trades

Auth (v2):
  {
    "action":"req",
    "ch":"auth",
    "params":{
      "authType":"api",
      "accessKey":<key>,
      "signatureMethod":"HmacSHA256",
      "signatureVersion":"2.1",
      "timestamp":"YYYY-MM-DDTHH:MM:SS",
      "signature":<HMAC base64 over "GET\\napi.huobi.pro\\n/ws/v2\\n<sorted-qs>">
    }
  }

Spot positions don't exist in the perp sense — for HTX we treat each
non-zero balance as the "position" for the corresponding asset. That
gives spot/short pair detection something to compare against (the user
holds 0.5 BTC spot + a short BTC perp on Bybit ⇒ pair).
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

from backend.services.user_streams._base import (
    BaseUserStream, EVT_BALANCE_UPDATE, EVT_POSITION_UPDATE, UserStreamEvent,
)

logger = logging.getLogger("avalant.userstream.htx")

WS_URL = "wss://api.huobi.pro/ws/v2"
HOST = "api.huobi.pro"
PATH = "/ws/v2"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _sign(method: str, host: str, path: str, params: dict, secret: str) -> str:
    items = sorted(params.items(), key=lambda kv: kv[0])
    qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in items)
    payload = f"{method.upper()}\n{host}\n{path}\n{qs}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.b64encode(sig).decode()


class HtxUserStream(BaseUserStream):
    name = "htx"
    # HTX gzips frames — _supervisor expects raw text/json, so we override
    # parse path via a wrapper; for now rely on the supervisor's recv logic.

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        if not creds.get("api_key") or not creds.get("api_secret"):
            raise RuntimeError("htx user-stream: missing api_key/api_secret")
        return WS_URL, {}

    @classmethod
    async def subscribe(cls, ws, creds: dict) -> None:
        api_key = (creds.get("api_key") or "").strip()
        api_secret = (creds.get("api_secret") or "").strip()
        ts = _ts()
        params = {
            "accessKey": api_key,
            "signatureMethod": "HmacSHA256",
            "signatureVersion": "2.1",
            "timestamp": ts,
        }
        sig = _sign("GET", HOST, PATH, params, api_secret)
        await ws.send(json.dumps({
            "action": "req",
            "ch": "auth",
            "params": {
                "authType": "api",
                **params,
                "signature": sig,
            },
        }))

        # Wait for auth response
        for _ in range(6):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                raise RuntimeError("htx auth: timeout")
            msg = cls._decode(raw)
            if not msg:
                continue
            ch = msg.get("ch") or ""
            if ch == "auth":
                if int(msg.get("code") or 0) != 200:
                    raise RuntimeError(f"htx auth failed: {msg}")
                break
            if msg.get("action") == "ping":
                # Reply to keepalive while waiting for auth
                await ws.send(json.dumps({"action": "pong", "data": msg.get("data")}))
        else:
            raise RuntimeError("htx auth: no response")

        # Subscribe to spot account updates (modes: 0=balance only, 1=available+balance, 2=full)
        await ws.send(json.dumps({
            "action": "sub",
            "ch": "accounts.update#2",
        }))

    @classmethod
    def _decode(cls, raw) -> dict | None:
        """HTX gzips frames; decompress + JSON-parse uniformly."""
        try:
            if isinstance(raw, (bytes, bytearray)):
                try:
                    decompressed = gzip.decompress(bytes(raw)).decode()
                except Exception:
                    decompressed = bytes(raw).decode(errors="ignore")
                return json.loads(decompressed)
            if isinstance(raw, str):
                return json.loads(raw)
        except Exception:
            return None
        return None

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        # Supervisor passes already-decoded dicts in most adapters; HTX
        # frames may still be raw bytes if gzip wasn't decoded upstream.
        msg = raw if isinstance(raw, dict) else cls._decode(raw)
        if not isinstance(msg, dict):
            return None

        # Heartbeat — caller should pong, but we don't have ws here. Return None
        # so the supervisor continues, and rely on websockets-lib pings.
        if msg.get("action") == "ping":
            return None

        ch = msg.get("ch") or ""
        if ch == "accounts.update#2":
            data = msg.get("data") or {}
            ccy = (data.get("currency") or "").upper()
            if not ccy:
                return None
            try:
                balance = float(data.get("balance") or 0)
                available = float(data.get("available") or 0)
            except (TypeError, ValueError):
                return None
            # Treat USDT/USDC as USD-like balance update; everything else as a
            # spot-position event so the supervisor / caller can reason about
            # spot/short pairs (the asset held + amount = the implicit position).
            if ccy in ("USDT", "USDC"):
                return UserStreamEvent(kind=EVT_BALANCE_UPDATE, balance_usdt=balance, raw=raw)
            if balance == 0 and available == 0:
                # Balance dropped to 0 — emit a flat position so the snapshot
                # forgets stale spot holdings on this asset.
                return UserStreamEvent(
                    kind=EVT_POSITION_UPDATE, symbol=ccy, qty=0.0, raw=raw,
                )
            return UserStreamEvent(
                kind=EVT_POSITION_UPDATE,
                symbol=ccy,
                side="buy",  # spot holding is implicit "long"
                qty=balance,
                margin_mode="spot",
                raw=raw,
            )
        return None
