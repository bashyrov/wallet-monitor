"""Bybit V5 private user-stream.

Flow:
  1. Connect wss://stream.bybit.com/v5/private
  2. Send auth frame: {op:"auth", args:[api_key, expires_ms, signature]}
       signature = HMAC_SHA256(secret, "GET/realtime" + expires_ms)
  3. On auth-success, subscribe: position + wallet + execution
  4. Push events arrive on those topics
  5. WS auto-keeps alive via 20s ping; Bybit sends pong. No
     listenKey-style renewal needed.

Reference: https://bybit-exchange.github.io/docs/v5/ws/connect#authentication
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

logger = logging.getLogger("avalant.userstream.bybit")

WS_URL = "wss://stream.bybit.com/v5/private"
AUTH_TTL_S = 60  # auth signature expires this many seconds in the future


class BybitUserStream(BaseUserStream):
    name = "bybit"

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        return WS_URL, {}

    @classmethod
    async def subscribe(cls, ws, creds: dict) -> None:
        api_key = (creds.get("api_key") or "").strip()
        api_secret = (creds.get("api_secret") or "").strip()
        if not api_key or not api_secret:
            raise RuntimeError("bybit user-stream: missing api_key/api_secret")
        expires = int((time.time() + AUTH_TTL_S) * 1000)
        prehash = f"GET/realtime{expires}"
        sig = hmac.new(api_secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()
        await ws.send(json.dumps({
            "op": "auth",
            "args": [api_key, expires, sig],
        }))
        # Wait for auth response (Bybit replies with op=auth + success bool).
        # Timeout each recv so a misbehaving connection doesn't hang the
        # supervisor task forever.
        for _ in range(5):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                raise RuntimeError("bybit auth: timeout waiting for response")
            try:
                msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except Exception:
                continue
            if msg.get("op") == "auth":
                if not msg.get("success", False):
                    raise RuntimeError(f"bybit auth failed: {msg}")
                break
        else:
            raise RuntimeError("bybit auth: no response after 5 frames")
        # Subscribe to live channels
        await ws.send(json.dumps({
            "op": "subscribe",
            "args": ["position", "wallet", "execution"],
        }))

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        if not isinstance(raw, dict):
            return None
        topic = raw.get("topic") or ""

        # Position update — fires whenever any position changes (open,
        # close, partial fill, mark price tick, leverage change). Each
        # frame may contain multiple positions; we emit one event per
        # frame here using the first relevant entry. The supervisor
        # dispatches events one at a time.
        if topic == "position":
            data = raw.get("data") or []
            if not data:
                return None
            # If multiple positions arrive in one frame, dispatch the
            # first one and queue the rest in the snapshot via the
            # event loop (we emit them by re-walking; for simplicity,
            # take the first — subsequent frames from Bybit usually
            # cover the others within ms).
            for p in data:
                sym_full = (p.get("symbol") or "").upper()
                if not sym_full.endswith("USDT"):
                    continue
                size_s = p.get("size") or "0"
                try:
                    size = float(size_s)
                except (TypeError, ValueError):
                    size = 0.0
                side_raw = (p.get("side") or "").lower()
                # Bybit returns "Buy" / "Sell" / "" (when flat). Use
                # explicit side rather than sign (one-way mode always
                # has size>=0).
                if size == 0 or side_raw not in ("buy", "sell"):
                    return UserStreamEvent(
                        kind=EVT_POSITION_UPDATE,
                        symbol=sym_full[:-4],
                        qty=0.0,
                        raw=raw,
                    )
                ep = p.get("entryPrice") or p.get("avgPrice")
                mp = p.get("markPrice")
                upnl = p.get("unrealisedPnl")
                lev = p.get("leverage")
                # tradeMode: 0=cross, 1=isolated (only in regular accounts;
                # UTA is cross-only and won't include this field)
                tm = p.get("tradeMode")
                margin_mode = None
                if tm == 1:
                    margin_mode = "isolated"
                elif tm == 0:
                    margin_mode = "cross"
                return UserStreamEvent(
                    kind=EVT_POSITION_UPDATE,
                    symbol=sym_full[:-4],
                    side=side_raw,
                    qty=abs(size),
                    entry_price=float(ep) if ep else None,
                    mark_price=float(mp) if mp else None,
                    unrealized_pnl_usd=float(upnl) if upnl else None,
                    leverage=int(float(lev)) if lev else None,
                    margin_mode=margin_mode,
                    raw=raw,
                )
            return None

        # Wallet update — Bybit pushes the full wallet snapshot per coin.
        # We only care about USDT for the trade-panel balance card.
        if topic == "wallet":
            data = raw.get("data") or []
            for acct in data:
                for coin in (acct.get("coin") or []):
                    if (coin.get("coin") or "").upper() != "USDT":
                        continue
                    bal = coin.get("walletBalance") or coin.get("equity")
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

        # Execution — order fills. Order History tab is fed by trade_orders
        # DB writes, so we don't surface these.
        if topic == "execution":
            return None

        # Subscribe ack / pong — ignore
        return None
