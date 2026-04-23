"""Per-exchange funding-WS worker (Fetcher split, milestone M4).

Same isolation story as the orderbook worker (M1/M2): one process owns one
exchange's funding stream, writes funding_ws.<ex>.json, master merges.

Usage (typically invoked by orderbook_ws_master.spawn_funding_workers):
    AVALANT_OWNED_FUNDING_EXCHANGE=binance python -m backend.services.funding_ws_worker
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from backend.logging_config import setup_logging


def _owned() -> str:
    ex = (os.environ.get("AVALANT_OWNED_FUNDING_EXCHANGE") or "").strip().lower()
    if not ex:
        print("AVALANT_OWNED_FUNDING_EXCHANGE is required", file=sys.stderr)
        raise SystemExit(2)
    return ex


async def _run(exchange: str) -> None:
    logger = logging.getLogger("avalant.funding_ws_worker")
    logger.info("funding worker starting for %s (pid=%d)", exchange, os.getpid())

    # start_funding_ws_manager picks up AVALANT_OWNED_FUNDING_EXCHANGE at
    # manager.py import time and (a) only starts that exchange's adapter,
    # (b) writes to funding_ws.<ex>.json, (c) holds a per-exchange file lock.
    from backend.services.funding_ws import (
        start_funding_ws_manager, stop_funding_ws_manager,
    )
    mgr = start_funding_ws_manager()
    if mgr is None:
        logger.error("funding worker %s could not acquire lock — exiting", exchange)
        raise SystemExit(4)

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            pass

    logger.info("funding worker %s up", exchange)
    try:
        await shutdown.wait()
    finally:
        logger.info("funding worker %s shutting down", exchange)
        stop_funding_ws_manager()
        logger.info("funding worker %s stopped", exchange)


def main() -> None:
    exchange = _owned()
    os.environ.setdefault("AVALANT_ROLE", "fetcher")
    setup_logging(f"funding-worker-{exchange}")

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
