"""Hyperliquid user-stream — public WS, address-based.

No auth handshake — Hyperliquid identifies users by their on-chain
address (which we already store in wallet credentials). Subscribe with
{type:"webData2", user:"0x..."} to receive a unified stream of
positions / balances / orders for that account.

Reference:
  https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket
"""
from __future__ import annotations

import json
import logging
from typing import Any

from backend.services.user_streams._base import (
    BaseUserStream, EVT_BALANCE_UPDATE, EVT_POSITION_UPDATE, UserStreamEvent,
)

logger = logging.getLogger("avalant.userstream.hyperliquid")

WS_URL = "wss://api.hyperliquid.xyz/ws"


class HyperliquidUserStream(BaseUserStream):
    name = "hyperliquid"

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        return WS_URL, {}

    @classmethod
    async def subscribe(cls, ws, creds: dict) -> None:
        # Hyperliquid uses a wallet address (no api_key/secret). The
        # existing trade adapter stores the address in the same
        # `api_key` field for consistency, but `wallet_address` is the
        # canonical key when present.
        addr = (creds.get("wallet_address") or creds.get("api_key") or "").strip()
        if not addr:
            raise RuntimeError("hyperliquid user-stream: missing wallet address")
        if not addr.startswith("0x"):
            raise RuntimeError(f"hyperliquid: invalid address shape: {addr[:10]}…")
        await ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "webData2", "user": addr},
        }))

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        if not isinstance(raw, dict):
            return None
        ch = raw.get("channel") or ""
        if ch != "webData2":
            return None
        data = raw.get("data") or {}
        # webData2 is a unified snapshot: clearinghouseState contains
        # positions + margin summary, spotState contains spot balances.
        clr = (data.get("clearinghouseState") or {})
        positions = clr.get("assetPositions") or []
        # We emit the first non-zero position as the event. Subsequent
        # changes will trigger more webData2 frames covering all current
        # positions.
        for entry in positions:
            pos = (entry.get("position") or {})
            coin = (pos.get("coin") or "").upper()
            szi = pos.get("szi")  # signed size string
            try:
                sz = float(szi) if szi is not None else 0.0
            except (TypeError, ValueError):
                sz = 0.0
            if sz == 0:
                continue
            side = "buy" if sz > 0 else "sell"
            ep = pos.get("entryPx")
            mark = pos.get("markPx") or pos.get("oraclePx")
            up = pos.get("unrealizedPnl")
            lev = (pos.get("leverage") or {}).get("value")
            # leverage type: "cross" or "isolated"
            mm = (pos.get("leverage") or {}).get("type")
            margin_mode = mm if mm in ("cross", "isolated") else None
            return UserStreamEvent(
                kind=EVT_POSITION_UPDATE,
                symbol=coin,
                side=side,
                qty=abs(sz),
                entry_price=float(ep) if ep else None,
                mark_price=float(mark) if mark else None,
                unrealized_pnl_usd=float(up) if up else None,
                leverage=int(float(lev)) if lev else None,
                margin_mode=margin_mode,
                raw=raw,
            )
        # No positions in this frame — emit a balance update from the
        # margin summary so we still have something to write.
        ms = (clr.get("marginSummary") or {})
        equity = ms.get("accountValue")
        try:
            bal = float(equity) if equity is not None else None
        except (TypeError, ValueError):
            bal = None
        if bal is not None:
            return UserStreamEvent(kind=EVT_BALANCE_UPDATE, balance_usdt=bal, raw=raw)
        return None
