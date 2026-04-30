"""Kraken Futures user-stream — challenge-based private WS.

Auth flow:
  1. Connect wss://futures.kraken.com/ws/v1
  2. Send {"event":"challenge","api_key":<key>}
  3. Server replies with {"event":"challenge","message":<challenge>}
  4. Sign that challenge: signed = HMAC-SHA512(b64_decode(secret),
     SHA256(challenge)), then base64 → this is `signed_challenge`.
  5. Subscribe with {"event":"subscribe","feed":"open_positions",
     "api_key":<key>, "original_challenge":<challenge>,
     "signed_challenge":<signed>}.

Channels:
  open_positions   — push of current open futures positions per account
  balances         — flex/cash balance updates

Reference: https://docs.kraken.com/api/docs/futures-api/websocket-v1
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from typing import Any

from backend.services.user_streams._base import (
    BaseUserStream, EVT_BALANCE_UPDATE, EVT_POSITION_UPDATE, UserStreamEvent,
)

logger = logging.getLogger("avalant.userstream.kraken")

WS_URL = "wss://futures.kraken.com/ws/v1"


def _denorm(pf_sym: str) -> str:
    s = pf_sym
    if s.startswith("PF_"):
        s = s[3:]
    if s.endswith("USD"):
        s = s[:-3]
    if s == "XBT":
        s = "BTC"
    return s


def _sign_challenge(api_secret: str, challenge: str) -> str:
    secret_decoded = base64.b64decode(api_secret)
    hashed = hashlib.sha256(challenge.encode()).digest()
    sig = hmac.new(secret_decoded, hashed, hashlib.sha512).digest()
    return base64.b64encode(sig).decode()


class KrakenUserStream(BaseUserStream):
    name = "kraken"

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        if not creds.get("api_key") or not creds.get("api_secret"):
            raise RuntimeError("kraken user-stream: missing api_key/api_secret")
        return WS_URL, {}

    @classmethod
    async def subscribe(cls, ws, creds: dict) -> None:
        api_key = (creds.get("api_key") or "").strip()
        api_secret = (creds.get("api_secret") or "").strip()
        await ws.send(json.dumps({"event": "challenge", "api_key": api_key}))

        # Wait for challenge response
        challenge: str | None = None
        for _ in range(8):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                raise RuntimeError("kraken challenge: timeout")
            try:
                msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except Exception:
                continue
            if msg.get("event") == "challenge" and msg.get("message"):
                challenge = msg["message"]
                break
            if msg.get("event") == "alert":
                raise RuntimeError(f"kraken challenge: alert {msg}")
        if not challenge:
            raise RuntimeError("kraken challenge: no response")

        signed = _sign_challenge(api_secret, challenge)

        # Subscribe to open positions + balances. Both are private feeds.
        for feed in ("open_positions", "balances"):
            await ws.send(json.dumps({
                "event": "subscribe",
                "feed": feed,
                "api_key": api_key,
                "original_challenge": challenge,
                "signed_challenge": signed,
            }))

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        if not isinstance(raw, dict):
            return None
        feed = raw.get("feed") or ""
        # open_positions: snapshot has "positions" array; updates push individual
        # position objects with the same shape.
        if feed in ("open_positions", "open_positions_snapshot"):
            positions = raw.get("positions")
            if positions is None:
                # Single-position update — fields at top level
                positions = [raw]
            for p in positions:
                if not isinstance(p, dict):
                    continue
                pf = p.get("instrument") or p.get("symbol") or ""
                if not pf.startswith("PF_"):
                    continue
                base = _denorm(pf)
                try:
                    qty = float(p.get("balance") or p.get("size") or 0)
                except (TypeError, ValueError):
                    qty = 0.0
                if qty == 0:
                    return UserStreamEvent(
                        kind=EVT_POSITION_UPDATE, symbol=base, qty=0.0, raw=raw,
                    )
                side = "buy" if qty > 0 else "sell"
                try:
                    entry = float(p.get("entry_price") or 0) or None
                except (TypeError, ValueError):
                    entry = None
                try:
                    upnl = float(p.get("unrealized_funding") or 0) + float(p.get("pnl") or 0)
                except (TypeError, ValueError):
                    upnl = None
                return UserStreamEvent(
                    kind=EVT_POSITION_UPDATE,
                    symbol=base,
                    side=side,
                    qty=abs(qty),
                    entry_price=entry,
                    unrealized_pnl_usd=upnl,
                    margin_mode="cross",
                    raw=raw,
                )
        if feed in ("balances", "balances_snapshot"):
            # flex_account is the multi-collateral account most users have.
            flex = raw.get("flex_account") or {}
            bv = flex.get("balance_value")
            if bv is None:
                # Fallback: cash account totals
                cash = (raw.get("cash_account") or {}).get("balances") or {}
                try:
                    bv = float(cash.get("USD") or 0) + float(cash.get("USDT") or 0)
                except (TypeError, ValueError):
                    return None
            try:
                bal_f = float(bv)
            except (TypeError, ValueError):
                return None
            return UserStreamEvent(kind=EVT_BALANCE_UPDATE, balance_usdt=bal_f, raw=raw)
        return None
