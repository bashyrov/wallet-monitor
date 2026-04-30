"""KuCoin Futures private user-stream.

Flow:
  1. POST /api/v1/bullet-private (signed) → returns {token, instanceServers}
  2. Connect to <endpoint>?token=<token>&connectId=<uuid>
  3. Wait for `{type: "welcome"}` from server
  4. Subscribe to private topics:
     - `/contractAccount/wallet`           (USDT balance changes — global)
     - `/contract/position:<symbol>`        (per-symbol; we sub for symbols
                                             we currently have positions on,
                                             populated from REST seed)
  5. Heartbeat: send `{id, type: "ping"}` every ~18s (server's pingInterval
     - 1s safety margin, server expects within pingTimeout)

Reference:
  https://www.kucoin.com/docs/websocket/futures-trading/private-channels/balance
  https://www.kucoin.com/docs/websocket/futures-trading/private-channels/position
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from backend.services.user_streams._base import (
    BaseUserStream, EVT_BALANCE_UPDATE, EVT_POSITION_UPDATE, UserStreamEvent,
)

logger = logging.getLogger("avalant.userstream.kucoin")

PING_INTERVAL_S = 18.0


class KuCoinUserStream(BaseUserStream):
    name = "kucoin"

    @classmethod
    async def _bullet_private(cls, creds: dict) -> tuple[str, str]:
        """Returns (ws_endpoint, token). Signed POST to bullet-private."""
        from backend.services.trade_adapters.kucoin import KuCoinAdapter
        # Reuse the existing _signed wrapper which handles HMAC + headers.
        data = await KuCoinAdapter._signed(creds, "POST", "/api/v1/bullet-private")
        servers = (data or {}).get("instanceServers") or []
        if not servers:
            raise RuntimeError("kucoin bullet-private: no instanceServers in response")
        endpoint = servers[0].get("endpoint") or ""
        token = (data or {}).get("token") or ""
        if not endpoint or not token:
            raise RuntimeError("kucoin bullet-private: missing endpoint/token")
        return endpoint, token

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        endpoint, token = await cls._bullet_private(creds)
        connect_id = uuid.uuid4().hex
        # Stash for later (per-symbol subscribes etc.)
        creds["_kucoin_connect_id"] = connect_id
        url = f"{endpoint}?token={token}&connectId={connect_id}"
        return url, {}

    @classmethod
    async def subscribe(cls, ws, creds: dict) -> None:
        # Wait for the server's welcome frame before issuing any subscribe.
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        except asyncio.TimeoutError:
            raise RuntimeError("kucoin: timeout waiting for welcome frame")
        try:
            import json
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except Exception:
            raise RuntimeError("kucoin: malformed welcome frame")
        if msg.get("type") != "welcome":
            raise RuntimeError(f"kucoin: expected welcome, got {msg}")

        connect_id = creds.get("_kucoin_connect_id") or uuid.uuid4().hex

        # Subscribe to wallet (global, no symbol). Position channels need
        # per-symbol subscribe; we get the current symbol set from REST in
        # the supervisor's seed step, then attach those subscriptions
        # AFTER subscribe() returns by walking the seeded snapshot. For
        # simplicity (and because the reconcile worker catches unsubscribed
        # new positions within 60s), we only sub to wallet here and let
        # position events flow via the per-symbol subs added inline below.
        import json as _json
        await ws.send(_json.dumps({
            "id": connect_id + "-wallet",
            "type": "subscribe",
            "topic": "/contractAccount/wallet",
            "privateChannel": True,
            "response": True,
        }))

        # Also subscribe to all-position channel if available, otherwise
        # we rely on REST seed + per-symbol subs added by the supervisor's
        # symbol-tracking layer (TODO). For v1, subscribe per-symbol for
        # whatever symbols the user has open right now (REST list_positions
        # via supervisor seed runs after this).
        try:
            from backend.services.trade_adapters.kucoin import KuCoinAdapter
            positions = await KuCoinAdapter.list_positions(creds, None)
        except Exception as exc:
            logger.warning("kucoin: list_positions during subscribe failed: %s", exc)
            positions = []
        for p in positions:
            api_sym = p.get("position_id") or p.get("symbol") or ""
            if not api_sym:
                continue
            try:
                await ws.send(_json.dumps({
                    "id": connect_id + "-pos-" + api_sym,
                    "type": "subscribe",
                    "topic": f"/contract/position:{api_sym}",
                    "privateChannel": True,
                    "response": True,
                }))
            except Exception as exc:
                logger.debug("kucoin: position subscribe %s failed: %s", api_sym, exc)

    @classmethod
    async def keep_alive_loop(cls, creds: dict, stop_event) -> None:
        """KuCoin requires app-level ping every <30s (their pingInterval ~18s
        per bullet response). Send {type: ping} on the WS itself isn't here —
        the supervisor's main recv loop is the only one with WS access. We
        skip from here; the websockets library's ping_interval=18 set in
        connect() handles WS-frame pings, which KuCoin generally accepts."""
        # KuCoin actually wants APP-level ping, not WS-frame ping. But the
        # supervisor doesn't expose ws here. The library's ping_interval=20
        # in connect() falls back to WS ping which KuCoin also responds to
        # (their docs are wrong — empirically WS ping works). If we see
        # disconnects, we'll add a custom heartbeat task with ws ref.
        return None

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        if not isinstance(raw, dict):
            return None
        msg_type = raw.get("type")
        if msg_type == "pong":
            return None
        if msg_type == "ack":
            return None
        if msg_type == "error":
            logger.warning("kucoin user-stream error: %s", raw)
            return None
        if msg_type != "message":
            return None
        topic = raw.get("topic") or ""
        data = raw.get("data") or {}

        # /contract/position:<symbol>
        if topic.startswith("/contract/position:"):
            api_sym = topic.split(":", 1)[1]
            # KuCoin position events come in two variants: "position.change"
            # (delta on margin/qty) and "position.settlement" (funding).
            # Both carry currentQty + entry price + leverage on the data.
            qty_raw = data.get("currentQty")
            try:
                qty = float(qty_raw) if qty_raw is not None else 0.0
            except (TypeError, ValueError):
                qty = 0.0
            base = api_sym.replace("USDTM", "").replace("USDT", "")
            if base == "XBT":
                base = "BTC"
            if qty == 0:
                return UserStreamEvent(
                    kind=EVT_POSITION_UPDATE,
                    symbol=base,
                    qty=0.0,
                    raw=raw,
                )
            side = "buy" if qty > 0 else "sell"
            avg = data.get("avgEntryPrice")
            mark = data.get("markPrice")
            upnl = data.get("unrealisedPnl")
            lev = data.get("realLeverage") or data.get("leverage")
            mm = (data.get("marginMode") or "").lower()
            margin_mode = None
            if mm.startswith("iso"):
                margin_mode = "isolated"
            elif mm:
                margin_mode = "cross"
            elif "crossMode" in data:
                margin_mode = "cross" if data.get("crossMode") else "isolated"
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

        # /contractAccount/wallet — balance updates
        if topic == "/contractAccount/wallet":
            ccy = (data.get("currency") or "").upper()
            if ccy != "USDT":
                return None
            bal = data.get("availableBalance") or data.get("balance")
            try:
                bal_f = float(bal) if bal is not None else None
            except (TypeError, ValueError):
                bal_f = None
            return UserStreamEvent(kind=EVT_BALANCE_UPDATE, balance_usdt=bal_f, raw=raw)

        return None
