"""Entry point for the data-plane sidecar.

Runs every background job that used to live inside a uvicorn worker's
event loop:

  · orderbook WS manager + prewarm + file dumper
  · funding-rate WS streams (11 exchanges) + owner dump
  · arb refresh loop (recompute + write arbitrage.json)
  · price refresh loop (USD price cache for alerts)
  · alert service (Telegram notifications on spreads)
  · telegram bot (login link commands)
  · alpha loops (health / snapshot / anomaly)

The web side (uvicorn) has ALL of these disabled via
`AVALANT_ROLE=web` and only runs HTTP/WS handlers that read the shared
cache files. Gives each process its own event loop — a misbehaving
external WS can no longer starve user requests.

Usage:
    python -m fetcher             # foreground
    AVALANT_ROLE=fetcher python -m fetcher
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from backend.logging_config import setup_logging, install_asyncio_hook

# Rotating files under <LOG_DIR>/fetcher/ + console + uncaught-exc hooks
setup_logging("fetcher")

logger = logging.getLogger("avalant.fetcher")


async def _run() -> None:
    install_asyncio_hook()
    # Mark role for any downstream checks
    os.environ.setdefault("AVALANT_ROLE", "fetcher")

    # Lazy imports so a partial env (missing DB URL, etc.) fails loud on
    # startup rather than at first request.
    from backend.db.base import init_db
    init_db()  # idempotent — creates tables if web hasn't yet run alembic

    # ── Price cache (alerts + portfolio read it via memo) ────────────
    from backend.services.price_service import start_price_loop, stop_price_loop
    start_price_loop()
    logger.info("fetcher: price loop started")

    # ── Orderbook prewarm: WS streams + file dumper ──────────────────
    from backend.services.orderbook_cache import start_prewarm, stop_prewarm
    start_prewarm()
    logger.info("fetcher: orderbook prewarm started")

    # ── Funding WS streams — 11 exchanges ────────────────────────────
    from backend.services.funding_ws import (
        start_funding_ws_manager, stop_funding_ws_manager,
    )
    start_funding_ws_manager()
    logger.info("fetcher: funding WS manager started")

    # ── Screener broadcaster's compute half (NOT the WS push half).
    # _refresh_loop writes arbitrage.json every 3s; the push half lives
    # on web workers where the WS client sets live.
    from backend.api.v1.screener import start_refresh_loop, stop_refresh_loop
    start_refresh_loop()
    logger.info("fetcher: screener refresh loop started")

    # ── Spot-short arbitrage refresh (same cadence as futures) ──────
    from backend.services.spot_arbitrage_service import (
        start_spot_refresh_loop, stop_spot_refresh_loop,
    )
    start_spot_refresh_loop()
    logger.info("fetcher: spot refresh loop started")

    # ── DEX-short arbitrage refresh (30 s — DexScreener rate-limited) ─
    from backend.services.dex_arbitrage_service import (
        start_dex_refresh_loop, stop_dex_refresh_loop,
    )
    start_dex_refresh_loop()
    logger.info("fetcher: dex refresh loop started")

    # ── Alerts (Telegram) — reads funding cache, sends on threshold ──
    from backend.services.alert_service import start_alert_service, stop_alert_service
    start_alert_service()
    logger.info("fetcher: alert service started")

    # ── TG bot — login deep-links + admin commands ───────────────────
    from backend.services.tg_bot_service import start_tg_bot, stop_tg_bot
    start_tg_bot()
    logger.info("fetcher: TG bot started")

    # ── Alpha loops (health / snapshot / anomaly) ────────────────────
    from backend.services.health_service import health_loop
    from backend.services.replay_service import snapshot_loop
    from backend.services.anomaly_service import anomaly_loop
    alpha_tasks = [
        asyncio.create_task(health_loop(interval_s=60)),
        asyncio.create_task(snapshot_loop(interval_s=60)),
        asyncio.create_task(anomaly_loop(interval_s=120)),
    ]
    logger.info("fetcher: alpha loops started")

    # ── Graceful shutdown on SIGTERM/SIGINT ───────────────────────────
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows / restricted env — fall back to default handler
            pass

    logger.info("fetcher: up (PID=%d)", os.getpid())
    try:
        await stop_event.wait()
    finally:
        logger.info("fetcher: shutting down")
        for t in alpha_tasks:
            t.cancel()
        for name, fn in (
            ("alert_service", stop_alert_service),
            ("tg_bot", stop_tg_bot),
            ("dex_refresh_loop", stop_dex_refresh_loop),
            ("spot_refresh_loop", stop_spot_refresh_loop),
            ("refresh_loop", stop_refresh_loop),
            ("funding_ws_manager", stop_funding_ws_manager),
            ("prewarm", stop_prewarm),
            ("price_loop", stop_price_loop),
        ):
            try:
                fn()
            except Exception:
                logger.exception("fetcher: stop %s failed", name)


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
