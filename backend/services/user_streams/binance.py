"""Binance USDT-M futures user-stream.

Flow:
  1. POST /fapi/v1/listenKey  →  { listenKey: "abc123..." }
  2. WS connect to wss://fstream.binance.com/ws/<listenKey>
  3. Receive ACCOUNT_UPDATE / ORDER_TRADE_UPDATE / ACCOUNT_CONFIG_UPDATE
  4. Every 30 min: PUT /fapi/v1/listenKey to extend (otherwise the key
     expires after 60 min and the WS silently disconnects).

Reference:
  https://binance-docs.github.io/apidocs/futures/en/#user-data-streams
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.services.user_streams._base import (
    BaseUserStream, EVT_BALANCE_UPDATE, EVT_POSITION_UPDATE, UserStreamEvent,
)

logger = logging.getLogger("avalant.userstream.binance")

REST_BASE = "https://fapi.binance.com"
WS_BASE = "wss://fstream.binance.com/ws"
LISTEN_KEY_RENEW_INTERVAL_S = 30 * 60  # 30 min — listenKey valid 60 min


class BinanceUserStream(BaseUserStream):
    name = "binance"
    rest_base = REST_BASE
    ws_base = WS_BASE

    @classmethod
    async def _post_listen_key(cls, creds: dict) -> str:
        from backend.providers.http import RetryClient
        async with RetryClient(timeout=10) as c:
            r = await c.post(
                f"{cls.rest_base}/fapi/v1/listenKey",
                headers={"X-MBX-APIKEY": creds["api_key"]},
            )
            r.raise_for_status()
            data = r.json()
            return data["listenKey"]

    @classmethod
    async def _put_listen_key(cls, creds: dict, listen_key: str) -> None:
        from backend.providers.http import RetryClient
        async with RetryClient(timeout=10) as c:
            r = await c.put(
                f"{cls.rest_base}/fapi/v1/listenKey",
                headers={"X-MBX-APIKEY": creds["api_key"]},
            )
            # 200 even if the key is identical — we just want it kept alive
            if r.status_code >= 400:
                logger.warning("%s listenKey PUT failed: %s %s",
                               cls.name, r.status_code, r.text[:200])

    @classmethod
    async def get_ws_url(cls, creds: dict) -> tuple[str, dict]:
        listen_key = await cls._post_listen_key(creds)
        # Stash for the keep-alive loop
        creds["_listen_key"] = listen_key
        return f"{cls.ws_base}/{listen_key}", {}

    @classmethod
    async def keep_alive_loop(cls, creds: dict, stop_event) -> None:
        """PUT /fapi/v1/listenKey every 30 min until stop_event fires."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(),
                                       timeout=LISTEN_KEY_RENEW_INTERVAL_S)
                return  # stop_event fired
            except asyncio.TimeoutError:
                pass
            lk = creds.get("_listen_key")
            if not lk:
                return
            try:
                await cls._put_listen_key(creds, lk)
                logger.debug("%s: listenKey renewed", cls.name)
            except Exception as exc:
                logger.warning("%s: listenKey renew failed: %s", cls.name, exc)

    @classmethod
    def parse_event(cls, raw: Any) -> UserStreamEvent | None:
        if not isinstance(raw, dict):
            return None
        evt_type = raw.get("e")

        # ACCOUNT_UPDATE — fires on balance / position changes.
        # We emit one event per position in the batch + one balance event.
        if evt_type == "ACCOUNT_UPDATE":
            a = raw.get("a") or {}
            # Balance: walletBalance for USDT
            for b in (a.get("B") or []):
                if (b.get("a") or "").upper() == "USDT":
                    try:
                        bal = float(b.get("wb") or 0)
                    except (TypeError, ValueError):
                        bal = None
                    return UserStreamEvent(
                        kind=EVT_BALANCE_UPDATE,
                        balance_usdt=bal,
                        raw=raw,
                    )
            # Positions are in `a["P"]`; we yield only one event at a time
            # so the supervisor's dispatch loop stays simple. If multiple
            # positions update at once, they'll arrive in subsequent
            # ACCOUNT_UPDATE frames.
            for p in (a.get("P") or []):
                sym_full = (p.get("s") or "").upper()
                if not sym_full.endswith("USDT"):
                    continue
                amt = float(p.get("pa") or 0)
                ep = p.get("ep")
                up = p.get("up")
                mp = p.get("mp")  # not always present
                pos_side = (p.get("ps") or "").upper()
                # In hedge mode, BOTH long and short positions for a symbol
                # come through as separate "P" entries. amt sign is reliable
                # in one-way mode; use ps in hedge mode.
                if pos_side in ("LONG", "SHORT"):
                    side = "buy" if pos_side == "LONG" else "sell"
                else:
                    side = "buy" if amt > 0 else "sell"
                return UserStreamEvent(
                    kind=EVT_POSITION_UPDATE,
                    symbol=sym_full[:-4],  # strip USDT suffix
                    side=side if amt != 0 else None,
                    qty=abs(amt),
                    entry_price=float(ep) if ep else None,
                    mark_price=float(mp) if mp else None,
                    unrealized_pnl_usd=float(up) if up else None,
                    raw=raw,
                )
            return None

        # ACCOUNT_CONFIG_UPDATE — leverage / marginType change. We don't
        # emit an event for this (the next ACCOUNT_UPDATE has the new
        # leverage on each position).
        if evt_type == "ACCOUNT_CONFIG_UPDATE":
            return None

        # ORDER_TRADE_UPDATE — order fill events. Order History tab is
        # backed by trade_orders DB writes from place_open_order /
        # close_position, so we don't need to emit anything here.
        if evt_type == "ORDER_TRADE_UPDATE":
            return None

        # listenKeyExpired — Binance tells us our key is dead. We do
        # NOT renew here — the supervisor's recv loop will time out
        # and reconnect from scratch.
        if evt_type == "listenKeyExpired":
            logger.warning("binance: listenKeyExpired received — supervisor will reconnect")
            return None

        return None
