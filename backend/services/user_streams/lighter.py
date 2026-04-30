"""Lighter zk-perp user-stream — public account_all channel.

Lighter's zk-rollup makes account state public-by-design — anyone can
query positions/balances by account_index, no authentication needed.
That simplifies the stream: subscribe to `account_all/<account_index>`
and parse position/asset diffs as they arrive.

URL: wss://mainnet.zklighter.elliot.ai/stream
Subscribe frame: {"type":"subscribe","channel":"account_all/<idx>"}
First push contains a full snapshot; subsequent pushes are diffs in the
same shape under `update.positions` / `update.assets`.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from backend.services.user_streams._base import (
    BaseUserStream, EVT_BALANCE_UPDATE, EVT_POSITION_UPDATE, UserStreamEvent,
)

logger = logging.getLogger("avalant.userstream.lighter")

WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"


def _account_index(creds: dict) -> int:
    raw = (creds.get("api_key") or "").strip()
    if not raw:
        raise RuntimeError("lighter user-stream: missing account_index (api_key)")
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"lighter user-stream: invalid account_index '{raw[:20]}'")


class LighterUserStream(BaseUserStream):
    name = "lighter"

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        # Validate creds early so a malformed api_key doesn't waste a TLS handshake.
        _account_index(creds)
        return WS_URL, {}

    @classmethod
    async def subscribe(cls, ws, creds: dict) -> None:
        idx = _account_index(creds)
        await ws.send(json.dumps({
            "type": "subscribe",
            "channel": f"account_all/{idx}",
        }))

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        if not isinstance(raw, dict):
            return None
        msg_type = (raw.get("type") or "").lower()
        if msg_type in ("connected", "ping", "pong"):
            return None
        # Both initial 'subscribed/account_all' and subsequent 'update/account_all'
        # carry the same shape under raw['positions'] / raw['assets'] (snapshot)
        # or raw['update']['positions'] / raw['update']['assets'] (diff).
        ch = (raw.get("channel") or "").lower()
        if "account_all" not in ch:
            return None

        if "update" in raw and isinstance(raw["update"], dict):
            payload = raw["update"]
        else:
            payload = raw

        positions = payload.get("positions") or {}
        if isinstance(positions, dict):
            position_iter = positions.values()
        elif isinstance(positions, list):
            position_iter = positions
        else:
            position_iter = []

        for p in position_iter:
            if not isinstance(p, dict):
                continue
            sym = (p.get("symbol") or "").upper()
            try:
                qty = float(p.get("position") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty == 0 or not sym:
                continue
            side = "buy" if (p.get("sign") == 1 or qty > 0) else "sell"
            try:
                entry = float(p.get("avg_entry_price") or 0) or None
            except (TypeError, ValueError):
                entry = None
            try:
                upnl = float(p.get("unrealized_pnl") or 0) or None
            except (TypeError, ValueError):
                upnl = None
            return UserStreamEvent(
                kind=EVT_POSITION_UPDATE,
                symbol=sym,
                side=side,
                qty=abs(qty),
                entry_price=entry,
                unrealized_pnl_usd=upnl,
                margin_mode="cross",
                raw=raw,
            )

        # No positions changed in this frame — emit a balance update if the
        # USDC asset is present so the snapshot has something to write.
        assets = payload.get("assets") or {}
        if isinstance(assets, dict):
            for a in assets.values():
                if not isinstance(a, dict):
                    continue
                if (a.get("symbol") or "").upper() not in ("USDC", "USDT"):
                    continue
                try:
                    bal = float(a.get("balance") or 0)
                except (TypeError, ValueError):
                    continue
                return UserStreamEvent(kind=EVT_BALANCE_UPDATE, balance_usdt=bal, raw=raw)
        return None
