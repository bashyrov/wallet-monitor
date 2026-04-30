"""OKX V5 private user-stream.

Flow:
  1. Connect wss://ws.okx.com:8443/ws/v5/private
  2. Send login: {op:"login", args:[{apiKey, passphrase, timestamp, sign}]}
       timestamp = unix seconds string
       sign = base64(HMAC_SHA256(secret, timestamp + "GET" + "/users/self/verify"))
  3. Wait for {event:"login", code:"0"}
  4. Subscribe: positions (SWAP) + account
  5. Push events arrive — quantity is in CONTRACTS, multiply by ctVal
     to convert to base-asset units the UI expects.

Reference:
  https://www.okx.com/docs-v5/en/#overview-websocket-login
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

logger = logging.getLogger("avalant.userstream.okx")

WS_URL = "wss://ws.okx.com:8443/ws/v5/private"
LOGIN_PREHASH_SUFFIX = "GET/users/self/verify"


class OKXUserStream(BaseUserStream):
    name = "okx"

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        return WS_URL, {}

    @classmethod
    async def subscribe(cls, ws, creds: dict) -> None:
        api_key = (creds.get("api_key") or "").strip()
        api_secret = (creds.get("api_secret") or "").strip()
        passphrase = (creds.get("api_passphrase") or "").strip()
        if not api_key or not api_secret or not passphrase:
            raise RuntimeError("okx user-stream: missing api_key/secret/passphrase")

        # Pre-warm the instrument cache BEFORE we subscribe — otherwise
        # the first position frames arrive with ctVal still 1.0 and we
        # write wrong quantities to the snapshot. Position frames stop
        # coming once positions are stable, so the first ones matter.
        try:
            from backend.services.trade_adapters.okx import _instruments
            await _instruments()
        except Exception as exc:
            logger.warning("okx user-stream: prewarm instruments failed: %s", exc)

        ts = str(int(time.time()))
        prehash = f"{ts}{LOGIN_PREHASH_SUFFIX}"
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
        # Wait for login response. OKX replies with {event:"login", code:"0"}
        # on success, code != "0" + msg on failure.
        for _ in range(5):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                raise RuntimeError("okx login: timeout waiting for response")
            try:
                msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except Exception:
                continue
            if msg.get("event") == "login":
                if msg.get("code") != "0":
                    raise RuntimeError(f"okx login failed: {msg.get('msg') or msg}")
                break
            if msg.get("event") == "error":
                raise RuntimeError(f"okx auth error: {msg.get('msg') or msg}")
        else:
            raise RuntimeError("okx login: no response after 5 frames")

        await ws.send(json.dumps({
            "op": "subscribe",
            "args": [
                {"channel": "positions", "instType": "SWAP"},
                {"channel": "account"},
            ],
        }))

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        if not isinstance(raw, dict):
            return None
        arg = raw.get("arg") or {}
        ch = arg.get("channel") or ""

        if ch == "positions":
            data = raw.get("data") or []
            if not data:
                return None
            for p in data:
                inst_id = (p.get("instId") or "").upper()
                if not inst_id.endswith("-USDT-SWAP"):
                    continue
                pos_str = p.get("pos") or "0"
                try:
                    pos_contracts = float(pos_str)
                except (TypeError, ValueError):
                    pos_contracts = 0.0
                # Convert contracts → base asset via ctVal. The position
                # payload often carries ctVal directly; fall back to the
                # cached instruments table when it doesn't (the existing
                # REST adapter does the same — its docstring notes the
                # bug it caused: e.g. DOGE qty came back as 0.15 instead
                # of 150 because ctVal silently defaulted to 1).
                ct_val = 0.0
                ctv_raw = p.get("ctVal")
                if ctv_raw:
                    try:
                        ct_val = float(ctv_raw)
                    except (TypeError, ValueError):
                        ct_val = 0.0
                if not ct_val:
                    try:
                        from backend.services.trade_adapters.okx import _INSTR_CACHE
                        info = (_INSTR_CACHE.get("data") or {}).get(inst_id)
                        if info:
                            ct_val = float(info.get("ctVal") or 1.0)
                    except Exception:
                        pass
                if not ct_val:
                    ct_val = 1.0
                qty = abs(pos_contracts) * ct_val
                # Strip "-USDT-SWAP" → "BTC"
                base = inst_id[: -len("-USDT-SWAP")]
                if pos_contracts == 0:
                    return UserStreamEvent(
                        kind=EVT_POSITION_UPDATE,
                        symbol=base,
                        qty=0.0,
                        raw=raw,
                    )
                pos_side = (p.get("posSide") or "net").lower()
                # OKX modes: "net" (one-way, sign of pos), "long" / "short"
                if pos_side == "long":
                    side = "buy"
                elif pos_side == "short":
                    side = "sell"
                else:
                    side = "buy" if pos_contracts > 0 else "sell"
                avg = p.get("avgPx")
                mark = p.get("markPx")
                upl = p.get("upl")
                lev = p.get("lever")
                mgn = (p.get("mgnMode") or "").lower()
                margin_mode = mgn if mgn in ("cross", "isolated") else None
                return UserStreamEvent(
                    kind=EVT_POSITION_UPDATE,
                    symbol=base,
                    side=side,
                    qty=qty,
                    entry_price=float(avg) if avg else None,
                    mark_price=float(mark) if mark else None,
                    unrealized_pnl_usd=float(upl) if upl else None,
                    leverage=int(float(lev)) if lev else None,
                    margin_mode=margin_mode,
                    raw=raw,
                )
            return None

        if ch == "account":
            data = raw.get("data") or []
            for acct in data:
                for d in (acct.get("details") or []):
                    if (d.get("ccy") or "").upper() != "USDT":
                        continue
                    bal = d.get("cashBal")
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

        # Subscribe ack / error — ignore here, error frames are caught
        # during subscribe()
        return None
