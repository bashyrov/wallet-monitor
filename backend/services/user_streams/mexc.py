"""MEXC Futures private user-stream.

Flow:
  1. Connect wss://contract.mexc.com/edge
  2. Send login: {method:"login", param:{apiKey, signature, reqTime}}
       signature = HMAC_SHA256(secret, api_key + req_time), hex-lowercase
  3. After login OK, subscribe:
     - sub.position (all positions, no symbol filter)
     - sub.account  (balance updates)
  4. Server pushes pong every ~30s; we send {method:"ping"} occasionally.

MEXC's WS docs are sparse and the API has been known to change without
notice. This adapter does best-effort parsing and falls back to REST
when something goes sideways.

Reference: https://mexcdevelop.github.io/apidocs/contract_v1_en/#websocket-private-channels
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from backend.services.user_streams._base import (
    BaseUserStream, EVT_BALANCE_UPDATE, EVT_POSITION_UPDATE, UserStreamEvent,
)

logger = logging.getLogger("avalant.userstream.mexc")

WS_URL = "wss://contract.mexc.com/edge"


class MEXCUserStream(BaseUserStream):
    name = "mexc"

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        return WS_URL, {}

    @classmethod
    async def subscribe(cls, ws, creds: dict) -> None:
        api_key = (creds.get("api_key") or "").strip()
        api_secret = (creds.get("api_secret") or "").strip()
        if not api_key or not api_secret:
            raise RuntimeError("mexc user-stream: missing api_key/api_secret")
        ts = str(int(time.time() * 1000))
        sign_msg = api_key + ts
        sig = hmac.new(api_secret.encode(), sign_msg.encode(), hashlib.sha256).hexdigest()
        await ws.send(json.dumps({
            "method": "login",
            "param": {
                "apiKey": api_key,
                "signature": sig,
                "reqTime": ts,
            },
        }))
        # Wait for login response
        for _ in range(5):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                raise RuntimeError("mexc login: timeout")
            try:
                msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except Exception:
                continue
            ch = msg.get("channel") or msg.get("method") or ""
            # Login response: {channel:"rs.login", data:"success"}
            if ch == "rs.login":
                if (msg.get("data") or "").lower() not in ("success", "ok"):
                    raise RuntimeError(f"mexc login failed: {msg}")
                break
            if ch == "rs.error":
                raise RuntimeError(f"mexc login error: {msg.get('data') or msg}")
        else:
            raise RuntimeError("mexc login: no response after 5 frames")

        # Subscribe to position + account changes (no symbol filter — MEXC
        # pushes all symbols on these channels)
        await ws.send(json.dumps({"method": "sub.position", "param": {}}))
        await ws.send(json.dumps({"method": "sub.account", "param": {}}))

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        if not isinstance(raw, dict):
            return None
        ch = raw.get("channel") or ""
        data = raw.get("data") or {}

        # push.position
        if ch == "push.position":
            sym_full = (data.get("symbol") or "").upper()
            # MEXC contract symbols are like "BTC_USDT"
            if "_USDT" in sym_full:
                base = sym_full.split("_")[0]
            else:
                base = sym_full
            qty_raw = data.get("holdVol") or data.get("position")
            try:
                qty = float(qty_raw) if qty_raw is not None else 0.0
            except (TypeError, ValueError):
                qty = 0.0
            position_type = data.get("positionType")  # 1 = long, 2 = short
            if qty == 0 or position_type not in (1, 2, "1", "2"):
                return UserStreamEvent(
                    kind=EVT_POSITION_UPDATE,
                    symbol=base,
                    qty=0.0,
                    raw=raw,
                )
            side = "buy" if int(position_type) == 1 else "sell"
            avg = data.get("holdAvgPrice")
            upnl = data.get("realised") or data.get("unrealizedPnl")
            lev = data.get("leverage")
            mt = (data.get("marginType") or "").lower()
            margin_mode = "isolated" if mt.startswith("iso") else ("cross" if mt else None)
            return UserStreamEvent(
                kind=EVT_POSITION_UPDATE,
                symbol=base,
                side=side,
                qty=abs(qty),
                entry_price=float(avg) if avg else None,
                unrealized_pnl_usd=float(upnl) if upnl else None,
                leverage=int(float(lev)) if lev else None,
                margin_mode=margin_mode,
                raw=raw,
            )

        # push.account / push.asset
        if ch in ("push.account", "push.asset"):
            ccy = (data.get("currency") or data.get("coin") or "").upper()
            if ccy != "USDT":
                return None
            bal = data.get("availableBalance") or data.get("balance")
            try:
                bal_f = float(bal) if bal is not None else None
            except (TypeError, ValueError):
                bal_f = None
            return UserStreamEvent(kind=EVT_BALANCE_UPDATE, balance_usdt=bal_f, raw=raw)

        return None
