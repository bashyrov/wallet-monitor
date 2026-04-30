"""Supervisor for per-(user, wallet) WS user-streams.

Runs in the fetcher process. Owns one asyncio.Task per stream. State
machine per stream:

  INIT      → opening connection
  LIVE      → WS connected, events flowing. Snapshot.set_status('LIVE')
  DEGRADED  → WS just dropped. Reconnecting, attempts 1..MAX_RECONNECT.
              Snapshot.set_status('DEGRADED'). Readers fall back to REST.
  DEAD      → reconnect exhausted. Snapshot.set_status('DEAD'). REST
              is sole source until the supervisor next ensures_running.

Reconnect strategy: exponential backoff with jitter, max 5 attempts:
  attempt 1 → wait 2 ± 0.5s
  attempt 2 → wait 4 ± 1s
  attempt 3 → wait 8 ± 2s
  attempt 4 → wait 16 ± 4s
  attempt 5 → wait 32 ± 8s
  total spread for 5 attempts is ~1 minute, mostly wait. After failing
  five we go DEAD and the next ensures_running tick (every 60s) will
  retry.

Reads in trade_service:
  if snapshot.get_status() == 'LIVE':
      return snapshot.get_positions(...)
  else:
      # REST as before
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

import websockets

from backend.services.user_streams import get_adapter
from backend.services.user_streams import _snapshot
from backend.services.user_streams._base import (
    EVT_BALANCE_UPDATE, EVT_POSITION_UPDATE, UserStreamEvent,
)

logger = logging.getLogger("avalant.userstream")


_RECONNECT_BACKOFF_S = [2.0, 4.0, 8.0, 16.0, 32.0]
_RECONNECT_JITTER = 0.25  # ±25%
_HEARTBEAT_INTERVAL_S = 30.0
# We rely on websockets' ping_interval=20s + ping_timeout=60s to detect
# dead connections (set on the connect() call). A separate "no message
# in N seconds" timeout was a bug — Bybit/OKX/Binance don't push anything
# when positions are stable, so a quiet stream is healthy. The library's
# ping/pong is the real liveness signal.
_WS_RECV_TIMEOUT_S = 600.0  # backstop only; library detects dead conn first


class StreamTask:
    """One supervised stream."""

    def __init__(self, user_id: int, wallet_id: int, exchange: str, creds: dict):
        self.user_id = user_id
        self.wallet_id = wallet_id
        self.exchange = exchange
        self.creds = creds
        self.task: asyncio.Task | None = None
        self.stop_event = asyncio.Event()
        self.state: str = "INIT"
        # Set if the venue refused our credentials. Permanent — supervisor
        # won't restart this stream until the user updates their key
        # (which produces a different wallet credentials blob).
        self.auth_failed: bool = False

    def __repr__(self) -> str:
        return f"<Stream user={self.user_id} wallet={self.wallet_id} ex={self.exchange} state={self.state}>"

    async def run(self) -> None:
        """Top-level lifecycle. Loop reconnects up to MAX_RECONNECT."""
        adapter = get_adapter(self.exchange)
        if adapter is None:
            logger.warning("userstream: no adapter for %s — skipping stream", self.exchange)
            self._set_state("DEAD")
            return

        # Startup stagger: spread the initial connect across a 0-3s window
        # so 5+ wallets coming up at once don't fire 5 simultaneous
        # listenKey/login REST calls and trip per-IP weight (Binance 418
        # comes from this exact pattern). Deterministic per-(user, wallet)
        # so retries land at the same offset rather than fluttering.
        startup_delay = ((self.user_id * 31 + self.wallet_id * 7) % 30) / 10.0
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=startup_delay)
            return  # stop_event fired during startup delay
        except asyncio.TimeoutError:
            pass

        try:
            while not self.stop_event.is_set():
                # Try to (re)connect
                connected = await self._run_one_session(adapter)
                if not connected:
                    # _run_one_session always returns after WS closes.
                    # Decide whether to retry.
                    if self._exhausted_reconnects():
                        logger.warning(
                            "userstream %s: reconnect exhausted, going DEAD (user=%s wallet=%s)",
                            self.exchange, self.user_id, self.wallet_id,
                        )
                        self._set_state("DEAD")
                        return
                    await self._reconnect_sleep()
                # If we successfully reconnected, _run_one_session will block
                # in recv loop again — when it returns, this while loop runs
                # the next attempt with fresh backoff state.
        except asyncio.CancelledError:
            logger.info("userstream %s: cancelled (user=%s wallet=%s)",
                        self.exchange, self.user_id, self.wallet_id)
            raise
        except Exception as exc:
            logger.exception("userstream %s: fatal error (user=%s wallet=%s): %s",
                             self.exchange, self.user_id, self.wallet_id, exc)
            self._set_state("DEAD")

    async def _run_one_session(self, adapter) -> bool:
        """One WS connection lifecycle. Returns True if we ever reached
        LIVE state. After WS closes, returns control so the outer loop
        can decide reconnect vs give-up."""
        try:
            ws_url, ws_headers = await adapter.get_ws_url(self.creds)
        except Exception as exc:
            # Detect "key invalid" / "IP banned" / "rate limited" so we
            # don't waste retries (and don't spam the auth endpoint —
            # which on Binance EXTENDS the rate-limit ban window with
            # each retry). Different venues use different status codes
            # but they all surface as the auth/setup step failing.
            msg = str(exc).lower()
            if any(s in msg for s in (
                "401", "403", "418", "banned",
                "invalid api", "api-key", "-2014", "-2015",
                "-1003",  # Binance rate-limit error code
                "signature", "unauthorized",
                # IP-whitelist patterns (kucoin "invalid request ip",
                # mexc "ip is not in the whitelist", bybit similar).
                # User must whitelist the new server IP in their key.
                "not in the whitelist", "not in whitelist",
                "invalid request ip", "ip address is not allowed",
                "trusted ip", "ip whitelist",
                "400002",  # kucoin invalid timestamp / ip
                "400006",  # kucoin invalid ip
            )):
                logger.warning(
                    "userstream %s: AUTH/RATE-LIMIT FAILED for user=%s wallet=%s — "
                    "marking DEAD (will retry after fetcher restart): %s",
                    self.exchange, self.user_id, self.wallet_id, str(exc)[:200],
                )
                self.auth_failed = True
                self._reconnect_attempt = len(_RECONNECT_BACKOFF_S)  # exhaust
                self._set_state("DEAD")
                return False
            logger.warning(
                "userstream %s: get_ws_url failed (user=%s): %s",
                self.exchange, self.user_id, exc,
            )
            self._set_state("DEGRADED")
            self._reconnect_attempt += 1
            return False

        logger.info(
            "userstream %s: connecting (user=%s wallet=%s)",
            self.exchange, self.user_id, self.wallet_id,
        )
        try:
            async with websockets.connect(
                ws_url,
                additional_headers=list(ws_headers.items()) if ws_headers else None,
                ping_interval=20,
                ping_timeout=60,
                max_size=2**20,
            ) as ws:
                # Login / subscribe frames if needed
                try:
                    await adapter.subscribe(ws, self.creds)
                except Exception as exc:
                    logger.warning("userstream %s: subscribe failed: %s", self.exchange, exc)
                    self._set_state("DEGRADED")
                    return False

                # Seed the snapshot from REST so a stable position visible
                # before we connected stays visible after we go LIVE. Most
                # venues only push diffs; without this seed, a stable pos
                # would silently disappear from /trade/positions until the
                # next change event.
                try:
                    await self._seed_from_rest()
                except Exception as exc:
                    logger.warning(
                        "userstream %s: REST seed failed (positions stay until first WS event): %s",
                        self.exchange, exc,
                    )

                # Reset reconnect attempt counter — we're LIVE
                self._reconnect_attempt = 0
                self._set_state("LIVE")
                logger.info(
                    "userstream %s: LIVE (user=%s wallet=%s)",
                    self.exchange, self.user_id, self.wallet_id,
                )

                # Spawn keep-alive (e.g. listenKey PUT every 30 min)
                ka_task = asyncio.create_task(adapter.keep_alive_loop(self.creds, self.stop_event))
                hb_task = asyncio.create_task(self._heartbeat_loop())

                try:
                    while not self.stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=_WS_RECV_TIMEOUT_S)
                        except asyncio.TimeoutError:
                            logger.warning(
                                "userstream %s: 60s without message, treating as dead (user=%s)",
                                self.exchange, self.user_id,
                            )
                            break
                        try:
                            # HTX pushes gzipped frames — adapters that need
                            # them decode in pong_for / parse_event themselves.
                            if isinstance(raw, (str, bytes)):
                                try:
                                    data = json.loads(raw)
                                except Exception:
                                    # Adapter may handle non-JSON (e.g. gzip
                                    # bytes); pass raw through.
                                    data = raw
                            else:
                                data = raw
                        except Exception:
                            continue
                        # Reply to app-level pings before dispatching to
                        # parse_event — HTX/Bitget keepalives need a {pong}
                        # within ~30s or the venue closes the socket.
                        try:
                            pong = adapter.pong_for(data) if hasattr(adapter, "pong_for") else None
                        except Exception:
                            pong = None
                        if pong is not None:
                            try:
                                await ws.send(pong)
                            except Exception:
                                pass
                            continue
                        try:
                            evt = adapter.parse_event(data)
                        except Exception as exc:
                            logger.debug("userstream %s: parse error: %s (raw=%.200s)",
                                         self.exchange, exc, str(data))
                            continue
                        if evt is not None:
                            self._dispatch_event(evt)
                finally:
                    ka_task.cancel()
                    hb_task.cancel()
                    for t in (ka_task, hb_task):
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "userstream %s: WS error (user=%s wallet=%s): %s",
                self.exchange, self.user_id, self.wallet_id, exc,
            )
            self._set_state("DEGRADED")
            self._reconnect_attempt += 1
            return False

        # Clean disconnect
        self._set_state("DEGRADED")
        self._reconnect_attempt += 1
        return True

    async def _heartbeat_loop(self) -> None:
        """Periodically refresh the LIVE status TTL so readers know
        we're still alive."""
        while not self.stop_event.is_set():
            if self.state == "LIVE":
                _snapshot.set_status(self.user_id, self.wallet_id, "LIVE")
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)

    def _dispatch_event(self, evt: UserStreamEvent) -> None:
        if evt.kind == EVT_POSITION_UPDATE:
            payload = {
                "exchange": self.exchange,
                "symbol": evt.symbol,
                "side": evt.side or "",
                "quantity": evt.qty,
                "entry_price": evt.entry_price,
                "mark_price": evt.mark_price,
                "unrealized_pnl_usd": evt.unrealized_pnl_usd,
                "leverage": evt.leverage,
                "margin_mode": evt.margin_mode,
                "position_id": evt.symbol,
                "_source": "ws",
                "_ts": time.time(),
            }
            _snapshot.update_position(
                self.user_id, self.wallet_id, self.exchange,
                evt.symbol or "", payload,
            )
        elif evt.kind == EVT_BALANCE_UPDATE:
            _snapshot.update_balance(self.user_id, self.wallet_id, evt.balance_usdt)

    _reconnect_attempt: int = 0

    def _exhausted_reconnects(self) -> bool:
        return self._reconnect_attempt >= len(_RECONNECT_BACKOFF_S)

    async def _reconnect_sleep(self) -> None:
        idx = max(0, min(self._reconnect_attempt - 1, len(_RECONNECT_BACKOFF_S) - 1))
        base = _RECONNECT_BACKOFF_S[idx]
        jitter = base * _RECONNECT_JITTER
        wait = base + random.uniform(-jitter, jitter)
        logger.info(
            "userstream %s: reconnect attempt %d in %.1fs (user=%s wallet=%s)",
            self.exchange, self._reconnect_attempt, wait,
            self.user_id, self.wallet_id,
        )
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass

    async def _seed_from_rest(self) -> None:
        """Fetch current positions via the REST trade adapter and write
        them into the snapshot. Runs once per (re)connect — bridges the
        gap between "WS just connected" and "first push event arrives"
        for venues that only push diffs (Binance, Bybit, OKX, Bitget all
        do this for stable positions)."""
        try:
            from backend.services.trade_adapters import ADAPTERS as REST_ADAPTERS
        except Exception:
            return
        adapter = REST_ADAPTERS.get(self.exchange)
        if adapter is None or not hasattr(adapter, "list_positions"):
            return
        try:
            rows = await adapter.list_positions(self.creds, None)
        except Exception as exc:
            # Don't fail the LIVE transition just because REST seed failed.
            # A typical case: Binance listenKey worked but income endpoint
            # is rate-limited. WS will still pick up changes from now on.
            logger.warning(
                "userstream %s: REST positions fetch failed: %s",
                self.exchange, exc,
            )
            return
        seeded = 0
        for r in (rows or []):
            sym = (r.get("symbol") or "").upper()
            if not sym:
                continue
            payload = {**r, "_source": "ws", "_ts": time.time()}
            _snapshot.update_position(
                self.user_id, self.wallet_id, self.exchange, sym, payload,
            )
            seeded += 1
        if seeded:
            logger.info(
                "userstream %s: REST-seeded %d position(s) (user=%s wallet=%s)",
                self.exchange, seeded, self.user_id, self.wallet_id,
            )

    def _set_state(self, state: str) -> None:
        prev, self.state = self.state, state
        if prev != state:
            _snapshot.set_status(self.user_id, self.wallet_id, state)
            logger.info(
                "userstream %s: %s → %s (user=%s wallet=%s)",
                self.exchange, prev, state, self.user_id, self.wallet_id,
            )


# ── Supervisor singleton ────────────────────────────────────────────────────
_streams: dict[tuple[int, int], StreamTask] = {}
_stop_supervisor = asyncio.Event() if False else None  # set in start()


async def _ensure_stream(user_id: int, wallet_id: int, exchange: str, creds: dict) -> None:
    key = (user_id, wallet_id)
    existing = _streams.get(key)
    if existing and existing.task and not existing.task.done():
        return  # already running
    task_obj = StreamTask(user_id, wallet_id, exchange, creds)
    task_obj.task = asyncio.create_task(task_obj.run())
    _streams[key] = task_obj


async def _stop_stream(user_id: int, wallet_id: int) -> None:
    key = (user_id, wallet_id)
    s = _streams.pop(key, None)
    if not s:
        return
    s.stop_event.set()
    if s.task:
        try:
            await asyncio.wait_for(s.task, timeout=3)
        except asyncio.TimeoutError:
            s.task.cancel()
        except Exception:
            pass
    _snapshot.clear_wallet(user_id, wallet_id)


async def _scan_and_sync() -> None:
    """Walk the DB once a minute, ensure a stream is running for every
    trade-enabled wallet that belongs to a CURRENTLY-ONLINE user and
    whose adapter we support. Stops streams for users who went offline
    (closed the tab / session expired) or whose wallets disappeared
    (archived / purpose changed)."""
    from backend.db.base import SessionLocal
    from backend.db.models import Wallet
    from backend.crypto import decrypt_credentials
    from backend.services.online_presence import online_user_ids

    online = online_user_ids()  # None = Redis unavailable → fail-open
    if online is not None and not online:
        # No one online — reap all running streams.
        for key in list(_streams.keys()):
            await _stop_stream(*key)
        return

    db = SessionLocal()
    try:
        q = (
            db.query(Wallet)
            .filter(
                Wallet.wallet_type == "exchange",
                Wallet.purpose.in_(("screener", "both")),
                Wallet.is_archived == False,  # noqa: E712
            )
        )
        if online is not None:
            q = q.filter(Wallet.user_id.in_(list(online)))
        wallets = q.all()
    finally:
        db.close()

    desired: set[tuple[int, int]] = set()
    for w in wallets:
        ex = (w.type_value or "").lower()
        if get_adapter(ex) is None:
            continue
        # Skip wallets we already gave up on (auth failed → DEAD). The
        # supervisor doesn't auto-retry until the user re-enters creds
        # (which causes a wallet update → new (user, wallet) key).
        existing = _streams.get((w.user_id, w.id))
        if existing and existing.state == "DEAD" and getattr(existing, "auth_failed", False):
            desired.add((w.user_id, w.id))  # keep marked, but no spawn
            continue
        desired.add((w.user_id, w.id))
        try:
            creds = decrypt_credentials(w.credentials or {})
        except Exception as exc:
            logger.warning("userstream: decrypt creds failed wallet=%s: %s", w.id, exc)
            continue
        if not creds.get("api_key") or not creds.get("api_secret"):
            # User has the wallet entry but not the actual API keys —
            # nothing to subscribe with. Skip silently.
            continue
        await _ensure_stream(w.user_id, w.id, ex, creds)

    # Reap anything not in `desired` (wallet removed OR user offline)
    for key in list(_streams.keys()):
        if key not in desired:
            await _stop_stream(*key)


async def _run_supervisor_loop() -> None:
    """Scan loop. 5s cadence so a freshly-logged-in user sees their
    streams come up within seconds rather than waiting a minute. Cost
    is trivial — one Redis SCAN + one Postgres query per cycle, both
    sub-millisecond at our scale. The 60s heartbeat-TTL on online
    presence dwarfs this; bursty logins don't trigger thrash because
    `_ensure_stream` is idempotent (no-op if already running)."""
    logger.info("userstream supervisor started (scan interval=5s)")
    while True:
        try:
            await _scan_and_sync()
        except Exception as exc:
            logger.exception("userstream supervisor scan failed: %s", exc)
        await asyncio.sleep(5.0)


def start_user_stream_supervisor() -> None:
    """Hook called from fetcher startup."""
    asyncio.create_task(_run_supervisor_loop())


async def stop_user_stream_supervisor() -> None:
    keys = list(_streams.keys())
    for k in keys:
        await _stop_stream(*k)
