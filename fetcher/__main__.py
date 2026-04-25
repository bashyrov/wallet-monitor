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
    # Two modes:
    #   single (default)       — one process runs every exchange's WS
    #                            manager and writes books.json directly.
    #   multiprocess (opt-in)  — AVALANT_FETCHER_MODE=multiprocess +
    #                            AVALANT_WORKER_EXCHANGES=list, spawns a
    #                            child per exchange and merges their
    #                            per-exchange books.<ex>.json files.
    from backend.services.orderbook_ws_master import (
        is_multiprocess_mode, start_workers_and_merger, stop_workers_and_merger,
        worker_exchanges,
    )
    from backend.services.orderbook_cache import start_prewarm, stop_prewarm

    if is_multiprocess_mode():
        start_workers_and_merger()
        logger.info(
            "fetcher: multiprocess orderbook workers started (%s)",
            ",".join(worker_exchanges()),
        )
        # Non-WS venues still need REST-prewarm in the master; start it with
        # dump disabled so we don't race the merger for books.json.
        # Multiprocess mode: workers dump books.<ex>.json for their assigned
        # exchanges, the master's prewarm dumps books.master.json for the
        # rest (spot WS adapters, Paradex — anything that lives only in
        # master's _book_cache). Merger reads both sets and produces the
        # canonical books.json.
        start_prewarm(dump_books=False, dump_to_master_file=True)
        logger.info("fetcher: orderbook prewarm (REST-only, no dump) alongside workers")
    else:
        start_prewarm()
        logger.info("fetcher: orderbook prewarm started (single-process)")

    # ── Funding WS streams — 11 exchanges ────────────────────────────
    from backend.services.funding_ws import (
        start_funding_ws_manager, stop_funding_ws_manager,
    )
    start_funding_ws_manager()
    logger.info("fetcher: funding WS manager started")

    # ── Plan expiry — downgrades users with plan_expires_at < now() ──
    from backend.services.plan_expiry_service import (
        start_plan_expiry_service, stop_plan_expiry_service,
    )
    start_plan_expiry_service()
    logger.info("fetcher: plan expiry service started")

    # ── Token registry — contract-address lookup for ticker-collision guard ──
    from backend.services.token_registry import (
        start_token_registry, stop_token_registry,
    )
    start_token_registry()
    logger.info("fetcher: token registry started")

    # ── Arb compute — optional out-of-process worker ─────────────────────────
    # Gated by AVALANT_ARB_COMPUTE_MODE. "subprocess" spawns a dedicated
    # process that reads funding.json and writes arbitrage.json without
    # competing for the master's GIL. Default (unset / "inline") keeps the
    # legacy in-master compute path untouched, so this is zero-risk when
    # disabled — we can toggle on/off via env at any time.
    from backend.services.arb_compute_service import (
        start_arb_compute_process, stop_arb_compute_process, is_subprocess_mode,
    )
    start_arb_compute_process()
    if is_subprocess_mode():
        logger.info("fetcher: arb compute subprocess started")
    else:
        logger.info("fetcher: arb compute subprocess disabled (legacy in-master path)")

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

    # ── Subscription expiry reminder daemon ──────────────────────────
    from backend.services.expiry_notifier_service import (
        start_expiry_notifier, stop_expiry_notifier,
    )
    start_expiry_notifier()
    logger.info("fetcher: expiry-notifier started")

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
            ("expiry_notifier", stop_expiry_notifier),
            ("dex_refresh_loop", stop_dex_refresh_loop),
            ("spot_refresh_loop", stop_spot_refresh_loop),
            ("refresh_loop", stop_refresh_loop),
            ("funding_ws_manager", stop_funding_ws_manager),
            ("plan_expiry", stop_plan_expiry_service),
            ("arb_compute_process", stop_arb_compute_process),
            ("token_registry", stop_token_registry),
            ("orderbook_workers", stop_workers_and_merger),
            ("prewarm", stop_prewarm),
            ("price_loop", stop_price_loop),
        ):
            try:
                fn()
            except Exception:
                logger.exception("fetcher: stop %s failed", name)


def main() -> None:
    # uvloop: drop-in replacement for asyncio's selector loop with 2-4× the
    # throughput. Measurable fix for the fetcher's event-loop saturation —
    # 11 WS adapters + orderbook pollers + arb compute + dumps were bumping
    # into scheduler overhead on the default selector loop. Falls through
    # to stock asyncio if uvloop isn't available (e.g. on macOS some CI).
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        logger.info("fetcher: uvloop active")
    except Exception as exc:
        logger.info("fetcher: uvloop unavailable (%s) — using stock asyncio", exc)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
