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

        # Step 1: login
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

        # Wait for login response. Up to 8 frames * 5s each — Gate often
        # sends an empty heartbeat or pong before the login result, and
        # the login channel ack arrives on the SAME `futures.login`
        # channel but with `event="api"` and a `header.status` field.
        # Log every frame at INFO so a future "gate login: timeout" tells
        # us what came back instead of just dying silently.
        last_seen: list[str] = []
        for attempt in range(8):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                raise RuntimeError(f"gate login: timeout after {attempt} frames; saw {last_seen}")
            try:
                msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except Exception:
                last_seen.append("<non-json>")
                continue
            ch = msg.get("channel") if isinstance(msg, dict) else ""
            ev = msg.get("event") if isinstance(msg, dict) else ""
            last_seen.append(f"{ch}:{ev}")
            if ch == "futures.login" and ev == "api":
                err = msg.get("error")
                if err:
                    raise RuntimeError(f"gate login failed: {err}")
                hdr = msg.get("header") or {}
                if hdr.get("status") and str(hdr.get("status")) != "200":
                    raise RuntimeError(f"gate login bad status: {hdr}")
                logger.info("gate user-stream: login ok (frames seen: %s)", last_seen)
                break
        else:
            raise RuntimeError(f"gate login: no api ack after 8 frames; saw {last_seen}")

        # Step 2: subscribe to positions + balances. After login, the
        # API key is bound to the connection — no per-subscribe auth.
        # Per docs, payload "!all" subscribes to all positions for the
        # account; without it, you'd have to enumerate every contract.
        ts2 = int(time.time())
        await ws.send(json.dumps({
            "time": ts2,
            "channel": "futures.positions",
            "event": "subscribe",
            "payload": ["!all"],
        }))
        await ws.send(json.dumps({
            "time": ts2,
            "channel": "futures.balances",
            "event": "subscribe",
            "payload": ["!all"],
        }))

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
