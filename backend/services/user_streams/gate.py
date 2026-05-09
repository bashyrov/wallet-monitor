"""Gate.io V4 futures private user-stream.

Flow:
  1. Connect wss://fx-ws.gateio.ws/v4/ws/usdt
  2. Send login frame on `futures.login` channel:
       {time, channel:"futures.login", event:"api",
        payload:{api_key, signature, timestamp, req_id}}
       signature = HMAC_SHA512_HEX(secret,
                     f"channel=futures.login&event=api&time={ts}")
  3. Wait for {channel:"futures.login", event:"api", error:null}
  4. Subscribe to futures.positions + futures.balances (no auth needed
     after login).

Reference:
  https://www.gate.io/docs/developers/apiv4/ws/en/#api-key-authentication
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

logger = logging.getLogger("avalant.userstream.gate")

WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"


def _sign_v4_ws(secret: str, channel: str, event: str, ts: int) -> str:
    msg = f"channel={channel}&event={event}&time={ts}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha512).hexdigest()


class GateUserStream(BaseUserStream):
    name = "gate"

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        return WS_URL, {}

    @classmethod
    async def subscribe(cls, ws, creds: dict) -> None:
        api_key = (creds.get("api_key") or "").strip()
        api_secret = (creds.get("api_secret") or "").strip()
        if not api_key or not api_secret:
            raise RuntimeError("gate user-stream: missing api_key/api_secret")

        ts = int(time.time())
        sig = _sign_v4_ws(api_secret, "futures.login", "api", ts)

        # Step 1: send login. Gate v4 accepts auth-on-channel-frame: the
        # `api_key` + `signature` can be embedded in the SUBSCRIBE call
        # itself (per docs each channel frame can carry auth). Sending
        # the login-channel frame is still recommended but Gate is
        # silent about its ack — observed in production: Gate sends one
        # empty pong-style frame and never an explicit `futures.login:api`
        # ack. Previous code waited for the ack and timed out. New
        # approach: fire login + immediately subscribe; if auth was bad,
        # the subscribe response carries the error.
        login_frame = {
            "time": ts,
            "channel": "futures.login",
            "event": "api",
            "payload": {
                "api_key": api_key,
                "signature": sig,
                "timestamp": str(ts),
                "req_id": "login-1",
            },
        }
        await ws.send(json.dumps(login_frame))

        # Brief opportunistic drain (1s × 1 frame) — pulls Gate's first
        # silent welcome out of the way before subscribe so parse_event
        # doesn't see it. If it times out, no big deal.
        try:
            await asyncio.wait_for(ws.recv(), timeout=1.0)
        except (asyncio.TimeoutError, Exception):
            pass

        # Step 2: subscribe with embedded auth (Gate accepts the same
        # api_key + signature inside subscribe payloads, redundant with
        # login but ensures auth even if login frame was dropped).
        # Per docs, payload "!all" subscribes to all positions for the
        # account; without it, you'd have to enumerate every contract.
        ts2 = int(time.time())
        sig_pos = _sign_v4_ws(api_secret, "futures.positions", "subscribe", ts2)
        sig_bal = _sign_v4_ws(api_secret, "futures.balances", "subscribe", ts2)
        await ws.send(json.dumps({
            "time": ts2,
            "channel": "futures.positions",
            "event": "subscribe",
            "payload": ["!all"],
            "auth": {
                "method": "api_key",
                "KEY": api_key,
                "SIGN": sig_pos,
            },
        }))
        await ws.send(json.dumps({
            "time": ts2,
            "channel": "futures.balances",
            "event": "subscribe",
            "payload": ["!all"],
            "auth": {
                "method": "api_key",
                "KEY": api_key,
                "SIGN": sig_bal,
            },
        }))
        logger.info("gate user-stream: subscribe sent (login fire-and-forget)")

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        if not isinstance(raw, dict):
            return None
        ch = raw.get("channel") or ""
        ev = raw.get("event") or ""
        if ev != "update":
            return None  # subscribe acks, login responses, etc.

        if ch == "futures.positions":
            data = raw.get("result") or []
            if isinstance(data, dict):
                data = [data]
            for p in data:
                contract = (p.get("contract") or "").upper()
                if not contract.endswith("_USDT"):
                    continue
                base = contract[:-5]
                size = p.get("size")
                try:
                    size_f = float(size) if size is not None else 0.0
                except (TypeError, ValueError):
                    size_f = 0.0
                if size_f == 0:
                    return UserStreamEvent(
                        kind=EVT_POSITION_UPDATE,
                        symbol=base,
                        qty=0.0,
                        raw=raw,
                    )
                # Gate's `size` is signed (negative = short, positive = long).
                # Quantity is in base-asset coins for USDT-margined contracts
                # — no ctVal multiplication needed (Gate exposes that already
                # in the `quanto_multiplier` if relevant; for plain perps
                # multiplier=1, size IS the coin amount).
                side = "buy" if size_f > 0 else "sell"
                avg = p.get("entry_price")
                mark = p.get("mark_price")
                upnl = p.get("unrealised_pnl") or p.get("pnl")
                lev = p.get("leverage")
                # Gate: cross_leverage_limit > 0 → cross, otherwise isolated
                cross_lev = p.get("cross_leverage_limit")
                if cross_lev is not None:
                    try:
                        margin_mode = "cross" if float(cross_lev) > 0 else "isolated"
                    except (TypeError, ValueError):
                        margin_mode = None
                else:
                    margin_mode = None
                return UserStreamEvent(
                    kind=EVT_POSITION_UPDATE,
                    symbol=base,
                    side=side,
                    qty=abs(size_f),
                    entry_price=float(avg) if avg else None,
                    mark_price=float(mark) if mark else None,
                    unrealized_pnl_usd=float(upnl) if upnl else None,
                    leverage=int(float(lev)) if lev else None,
                    margin_mode=margin_mode,
                    raw=raw,
                )
            return None

        if ch == "futures.balances":
            data = raw.get("result") or []
            if isinstance(data, dict):
                data = [data]
            for b in data:
                bal = b.get("balance")
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
