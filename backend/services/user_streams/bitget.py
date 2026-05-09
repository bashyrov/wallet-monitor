"""Bitget V2 mix-private user-stream.

Flow:
  1. Connect wss://ws.bitget.com/v2/ws/private
  2. Send login: {op:"login", args:[{apiKey, passphrase, timestamp, sign}]}
       sign = base64(HMAC_SHA256(secret, ts + "GET" + "/user/verify"))
  3. Wait for {event:"login", code:"0"}
  4. Subscribe to positions + account on USDT-FUTURES product

Reference: https://www.bitget.com/api-doc/contract/websocket/private/Login-Channel
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from backend.services.user_streams._base import (
    BaseUserStream, EVT_BALANCE_UPDATE, EVT_POSITION_UPDATE, UserStreamEvent,
)

logger = logging.getLogger("avalant.userstream.bitget")

WS_URL = "wss://ws.bitget.com/v2/ws/private"
LOGIN_PREHASH_PATH = "/user/verify"


class BitgetUserStream(BaseUserStream):
    name = "bitget"
    # Bitget V2 private WS server doesn't send pings — closes idle
    # connections after ~30-60s. Per docs: client should send TEXT
    # "ping" every 30s. We use 25s for safety margin.
    ws_ping_interval_s = 25.0

    @classmethod
    def ws_ping_payload(cls):
        return "ping"

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        return WS_URL, {}

    @classmethod
    async def subscribe(cls, ws, creds: dict) -> None:
        api_key = (creds.get("api_key") or "").strip()
        api_secret = (creds.get("api_secret") or "").strip()
        passphrase = (creds.get("api_passphrase") or "").strip()
        if not api_key or not api_secret or not passphrase:
            raise RuntimeError("bitget user-stream: missing api_key/secret/passphrase")
        ts = str(int(time.time()))
        prehash = f"{ts}GET{LOGIN_PREHASH_PATH}"
        sig = base64.b64encode(
            hmac.new(api_secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        await ws.send(json.dumps({
            "op": "login",
            "args": [{
                "apiKey": api_key,
                "passphrase": passphrase,
                "timestamp": ts,
                "sign": sig,
            }],
        }))
        for _ in range(5):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                raise RuntimeError("bitget login: timeout")
            try:
                msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except Exception:
                continue
            if msg.get("event") == "login":
                if msg.get("code") not in ("0", 0):
                    raise RuntimeError(f"bitget login failed: {msg.get('msg') or msg}")
                break
            if msg.get("event") == "error":
                raise RuntimeError(f"bitget login error: {msg.get('msg') or msg}")
        else:
            raise RuntimeError("bitget login: no response after 5 frames")

        # Subscribe to positions + account on USDT-FUTURES product type.
        # instId = "default" subscribes to all symbols.
        await ws.send(json.dumps({
            "op": "subscribe",
            "args": [
                {"instType": "USDT-FUTURES", "channel": "positions", "instId": "default"},
                {"instType": "USDT-FUTURES", "channel": "account", "coin": "default"},
            ],
        }))

    @classmethod
    def pong_for(cls, msg) -> str | None:
        """Bitget V2 private WS expects TEXT "ping" → TEXT "pong" every
        ~30s. Without it the server closes after ~30-60s — observed as
        recurring `WS error: no close frame received or sent` flap on
        bitget user-stream. Same root cause as bingx/mexc fix."""
        if isinstance(msg, str) and msg.strip().lower() == "ping":
            return "pong"
        if isinstance(msg, bytes) and msg.strip().lower() == b"ping":
            return "pong"
        return None

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        if not isinstance(raw, dict):
            return None
        arg = raw.get("arg") or {}
        ch = arg.get("channel") or ""
        action = raw.get("action") or ""

        if ch == "positions":
            data = raw.get("data") or []
            for p in data:
                inst_id = (p.get("instId") or "").upper()
                if not inst_id.endswith("USDT"):
                    continue
                size_s = p.get("total") or p.get("openSize") or "0"
                try:
                    qty = float(size_s)
                except (TypeError, ValueError):
                    qty = 0.0
                hold_side = (p.get("holdSide") or "").lower()
                base = inst_id[:-4]
                if qty == 0 or hold_side not in ("long", "short"):
                    return UserStreamEvent(
                        kind=EVT_POSITION_UPDATE,
                        symbol=base,
                        qty=0.0,
                        raw=raw,
                    )
                side = "buy" if hold_side == "long" else "sell"
                avg = p.get("openPriceAvg") or p.get("averageOpenPrice")
                mark = p.get("markPrice")
                upnl = p.get("unrealizedPL")
                lev = p.get("leverage")
                mm = (p.get("marginMode") or "").lower()
                margin_mode = mm if mm in ("crossed", "cross", "isolated") else None
                # Bitget calls it "crossed"; normalize
                if margin_mode == "crossed":
                    margin_mode = "cross"
                return UserStreamEvent(
                    kind=EVT_POSITION_UPDATE,
                    symbol=base,
                    side=side,
                    qty=abs(qty),
                    entry_price=float(avg) if avg else None,
                    mark_price=float(mark) if mark else None,
                    unrealized_pnl_usd=float(upnl) if upnl else None,
                    leverage=int(float(lev)) if lev else None,
                    margin_mode=margin_mode,
                    raw=raw,
                )
            return None

        if ch == "account":
            data = raw.get("data") or []
            for acct in data:
                if (acct.get("marginCoin") or "").upper() != "USDT":
                    continue
                bal = acct.get("available") or acct.get("equity")
                try:
                    bal_f = float(bal) if bal is not None else None
                except (TypeError, ValueError):
                    bal_f = None
                return UserStreamEvent(
                    kind=EVT_BALANCE_UPDATE,
                    balance_usdt=bal_f,
                    raw=raw,
                )
            return None

        return None
