"""Backpack Exchange private user-stream.

Backpack uses ED25519 signing for both REST and WS. The WS sub frame
must include a per-stream signature with `instruction=subscribe`.

WS endpoint: wss://ws.backpack.exchange/

Authenticated streams:
  - account.orderUpdate
  - account.positionUpdate
  - account.balanceUpdate

Subscribe with signature:
  {
    "method": "SUBSCRIBE",
    "params": ["account.positionUpdate", "account.balanceUpdate"],
    "signature": [
      "<api_key>",        # base64
      "<signature_b64>",  # ed25519 sign of "instruction=subscribe&timestamp=<ts>&window=5000"
      "<timestamp_ms>",
      "<window_ms>"       # 5000
    ]
  }
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

from backend.services.user_streams._base import (
    BaseUserStream, EVT_BALANCE_UPDATE, EVT_POSITION_UPDATE, UserStreamEvent,
)

logger = logging.getLogger("avalant.userstream.backpack")

WS_URL = "wss://ws.backpack.exchange/"
RECV_WINDOW_MS = 5000


def _sign_subscribe(api_secret_b64: str) -> tuple[str, str, str]:
    """Returns (signature, timestamp, window). instruction=subscribe."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    ts = int(time.time() * 1000)
    msg = urlencode([
        ("instruction", "subscribe"),
        ("timestamp", str(ts)),
        ("window", str(RECV_WINDOW_MS)),
    ])
    seed = base64.b64decode(api_secret_b64)
    pk = Ed25519PrivateKey.from_private_bytes(seed)
    sig = pk.sign(msg.encode())
    return base64.b64encode(sig).decode(), str(ts), str(RECV_WINDOW_MS)


class BackpackUserStream(BaseUserStream):
    name = "backpack"

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        return WS_URL, {}

    @classmethod
    async def subscribe(cls, ws, creds: dict) -> None:
        api_key = (creds.get("api_key") or "").strip()
        api_secret = (creds.get("api_secret") or "").strip()
        if not api_key or not api_secret:
            raise RuntimeError("backpack user-stream: missing api_key/api_secret")
        try:
            sig, ts, window = _sign_subscribe(api_secret)
        except Exception as exc:
            raise RuntimeError(f"backpack user-stream: ed25519 sign failed: {exc}")
        await ws.send(json.dumps({
            "method": "SUBSCRIBE",
            "params": [
                "account.positionUpdate",
                "account.balanceUpdate",
            ],
            "signature": [api_key, sig, ts, window],
        }))

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        if not isinstance(raw, dict):
            return None
        # Backpack pushes unsolicited stream messages with `stream` and `data`
        # fields after subscribe.
        stream = raw.get("stream") or ""
        data = raw.get("data") or {}
        if not stream:
            return None

        if stream == "account.positionUpdate":
            symbol = (data.get("s") or data.get("symbol") or "").upper()
            # Backpack uses "BTC_USDC_PERP" or similar; strip suffix to base
            if "_" in symbol:
                base = symbol.split("_")[0]
            else:
                base = symbol
            qty_raw = data.get("Q") or data.get("netQuantity") or data.get("quantity")
            try:
                qty = float(qty_raw) if qty_raw is not None else 0.0
            except (TypeError, ValueError):
                qty = 0.0
            if qty == 0:
                return UserStreamEvent(
                    kind=EVT_POSITION_UPDATE,
                    symbol=base,
                    qty=0.0,
                    raw=raw,
                )
            side = "buy" if qty > 0 else "sell"
            avg = data.get("p") or data.get("entryPrice")
            mark = data.get("M") or data.get("markPrice")
            upnl = data.get("u") or data.get("unrealizedPnl")
            lev = data.get("l") or data.get("leverage")
            return UserStreamEvent(
                kind=EVT_POSITION_UPDATE,
                symbol=base,
                side=side,
                qty=abs(qty),
                entry_price=float(avg) if avg else None,
                mark_price=float(mark) if mark else None,
                unrealized_pnl_usd=float(upnl) if upnl else None,
                leverage=int(float(lev)) if lev else None,
                raw=raw,
            )

        if stream == "account.balanceUpdate":
            ccy = (data.get("a") or data.get("asset") or "").upper()
            # Backpack uses USDC for stablecoin balance on perp. Treat that as
            # the equivalent of "USDT" for our trade panel display.
            if ccy not in ("USDT", "USDC"):
                return None
            bal = data.get("d") or data.get("delta") or data.get("balance")
            try:
                bal_f = float(bal) if bal is not None else None
            except (TypeError, ValueError):
                bal_f = None
            return UserStreamEvent(kind=EVT_BALANCE_UPDATE, balance_usdt=bal_f, raw=raw)

        return None
