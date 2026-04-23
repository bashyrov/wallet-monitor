"""Per-exchange orderbook WS worker (Fetcher split, milestone M1).

Runs a WSManager for ONE exchange in its own process. Its event loop is
isolated from the master fetcher's, so a stall in any other exchange
(e.g. KuCoin JWT rotation blocking the loop for 8s) can't cascade into
this worker's heartbeats.

Usage (manual):
    AVALANT_OWNED_EXCHANGE=binance python -m backend.services.orderbook_ws_worker

Required env:
    AVALANT_OWNED_EXCHANGE — the exchange id to serve (binance / bybit / ...).

Optional:
    AVALANT_WORKER_SYMBOLS — space-separated symbols to subscribe on start.
                              If unset, worker starts idle and waits for the
                              shared pending_subs.json queue to be written.

State written:
    /tmp/avalant_cache/books.<exchange>.json — same format as books.json
                                                (per-exchange slice).

Shutdown: SIGTERM / SIGINT drain the WS connection, flush dump, exit.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading

from backend.logging_config import setup_logging


def _owned_exchange() -> str:
    ex = (os.environ.get("AVALANT_OWNED_EXCHANGE") or "").strip().lower()
    if not ex:
        print("AVALANT_OWNED_EXCHANGE is required", file=sys.stderr)
        raise SystemExit(2)
    return ex


async def _run(exchange: str) -> None:
    # Try uvloop where available — same 2-4x throughput win as in the main
    # fetcher process.
    try:
        import uvloop  # type: ignore
        # Only the top-level asyncio.run can set the loop policy; once we're
        # inside _run the loop already exists. If main() installed uvloop
        # earlier, great; otherwise we're on stock asyncio, which is fine.
        uvloop  # noqa: F401
    except Exception:
        pass

    logger = logging.getLogger("avalant.orderbook_worker")
    logger.info("worker starting for %s (pid=%d)", exchange, os.getpid())

    # Avoid double-start of the prewarm owner: we own a per-exchange lock,
    # not the main prewarm lock. Instead we drive the WS manager directly.
    from backend.services.orderbook_ws import (
        start_ws_manager, stop_ws_manager, is_ws_supported,
    )
    if not is_ws_supported(exchange):
        logger.error("exchange %s does not have a WS adapter — refusing to start", exchange)
        raise SystemExit(3)

    mgr = start_ws_manager()

    # Optional seed symbols — kicks the subscribe immediately rather than
    # waiting for the hotlist loop to hit its first tick.
    seed = (os.environ.get("AVALANT_WORKER_SYMBOLS") or "").strip()
    if seed:
        symbols = [s.upper() for s in seed.split() if s.strip()]
        if symbols:
            mgr.subscribe(exchange, symbols)
            logger.info("worker seeded %d symbols: %s", len(symbols), symbols[:10])

    # Run the standard prewarm hotlist loop — scoped to our exchange via the
    # AVALANT_OWNED_EXCHANGE env check inside _prewarm_hotlist_loop. This is
    # what lands the top-N arb symbols in our WSManager; without it the
    # worker would sit idle waiting for /ws/book subscribes.
    from backend.services.orderbook_cache import _prewarm_hotlist_loop
    hotlist_task = asyncio.create_task(_prewarm_hotlist_loop())

    # Reuse the pure-thread dumper from orderbook_cache. The _OWNED_EXCHANGE
    # env var (picked up at module import) narrows its output to our
    # books.<exchange>.json file, so the master can merge.
    from backend.services.orderbook_cache import (
        _prewarm_dump_loop_sync, _PER_EX_BOOKS_FILE,
    )
    assert _PER_EX_BOOKS_FILE, "per-exchange output path not initialised"
    stop_evt = threading.Event()
    dump_thread = threading.Thread(
        target=_prewarm_dump_loop_sync,
        args=(stop_evt,),
        name=f"worker-dump-{exchange}",
        daemon=True,
    )
    dump_thread.start()

    # Drain pending_subs.json entries targeted at us on a slow cadence. This
    # is the coarse-grained path the master uses to forward /ws/book
    # subscribe requests from the web role.
    async def _drain_pending():
        from backend.services.orderbook_cache import drain_pending_subs
        while not stop_evt.is_set():
            try:
                pending = drain_pending_subs()
                syms = pending.get(exchange) or []
                if syms:
                    mgr.subscribe(exchange, syms)
                    logger.info("worker added %d subs via pending_subs: %s", len(syms), syms[:5])
            except Exception as exc:
                logger.debug("drain_pending failed: %s", exc)
            try:
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break

    pending_task = asyncio.create_task(_drain_pending())

    # Graceful shutdown on SIGTERM/SIGINT.
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            pass

    logger.info("worker %s up — dumping to %s", exchange, _PER_EX_BOOKS_FILE)
    try:
        await shutdown.wait()
    finally:
        logger.info("worker %s shutting down", exchange)
        pending_task.cancel()
        hotlist_task.cancel()
        stop_evt.set()
        stop_ws_manager()
        dump_thread.join(timeout=3.0)
        logger.info("worker %s stopped", exchange)


def main() -> None:
    # Set role + exchange BEFORE any backend module imports so
    # orderbook_cache picks up _OWNED_EXCHANGE at its module level.
    exchange = _owned_exchange()
    os.environ.setdefault("AVALANT_ROLE", "fetcher")
    setup_logging(f"orderbook-worker-{exchange}")

    # uvloop at the loop-policy level, before asyncio.run — matches fetcher/__main__.
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except Exception:
        pass

    try:
        asyncio.run(_run(exchange))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
