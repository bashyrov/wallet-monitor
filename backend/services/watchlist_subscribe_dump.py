"""Dump active watchlist pairs to a shared JSON file so the Go
symbol-manager can subscribe orderbooks for them — even when the pair
isn't in the top-N arb feed.

Without this, a user-watched pair that drops out of the top loses its
live in_pct (the only Source of truth for Live Spread on /arb /
/watchlist after the mark→in_pct switch). With it, the symbol manager
unions watchlist symbols with the top-N tracked set.

Output shape: see Go's symbols.Manager loader for the matching reader.
We keep it deliberately tiny — one row per (sym, long_ex, short_ex),
no per-user attribution since the manager just unions across users.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import Any

from backend.db.base import SessionLocal
from backend.db.models import WatchlistItem

logger = logging.getLogger("avalant.watchlist_dump")

_CACHE_DIR = os.environ.get("AVALANT_CACHE_DIR", "/tmp/avalant_cache")
_OUT_FILE = os.path.join(_CACHE_DIR, "watchlist_subscribe.json")
_INTERVAL_S = 30.0

_task: asyncio.Task | None = None
_running = False


def _collect() -> list[dict[str, Any]]:
    """Distinct (symbol, long_ex, short_ex) across:
       - all users' active /watchlist rows
       - all active arb trigger orders (pending|firing|scheduled)

    Both sources are unioned and de-duped so the Go symbol manager
    keeps an orderbook flowing for any pair where work is pending —
    a stale book means trigger_order_service can't evaluate, so a TP
    sitting idle for hours still needs live data the moment spread
    crosses target.
    """
    from backend.db.models import ArbTriggerOrder
    db = SessionLocal()
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []

    def _add(sym: str | None, le: str | None, se: str | None) -> None:
        if not sym or not le or not se:
            return
        key = (sym.upper(), le.lower(), se.lower())
        if key in seen:
            return
        seen.add(key)
        out.append({"symbol": key[0], "long_exchange": key[1], "short_exchange": key[2]})

    try:
        # 1) Watchlist
        for sym, le, se in (
            db.query(
                WatchlistItem.symbol,
                WatchlistItem.long_exchange,
                WatchlistItem.short_exchange,
            ).distinct().all()
        ):
            _add(sym, le, se)

        # 2) Active triggers — every kind that needs the orderbook to
        #    evaluate. open + close eval against the pair they target;
        #    tp/sl inherit pair from arb_position so we walk those too.
        for sym, le, se in (
            db.query(
                ArbTriggerOrder.long_symbol,
                ArbTriggerOrder.long_exchange,
                ArbTriggerOrder.short_exchange,
            )
            .filter(
                ArbTriggerOrder.status.in_(("pending", "firing", "scheduled")),
                ArbTriggerOrder.long_exchange.isnot(None),
                ArbTriggerOrder.short_exchange.isnot(None),
            )
            .distinct().all()
        ):
            _add(sym, le, se)

        # tp/sl rows often leave long_exchange null (inherited from
        # arb_position). Pull them via the join.
        from backend.db.models import ArbPosition
        for sym, le, se in (
            db.query(
                ArbPosition.long_symbol,
                ArbPosition.long_exchange,
                ArbPosition.short_exchange,
            )
            .join(
                ArbTriggerOrder,
                ArbTriggerOrder.arb_position_id == ArbPosition.id,
            )
            .filter(
                ArbTriggerOrder.status.in_(("pending", "firing", "scheduled")),
            )
            .distinct().all()
        ):
            _add(sym, le, se)

        return out
    finally:
        db.close()


def _write_atomic(payload: list[dict[str, Any]]) -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    body = {
        "pairs": payload,
        "ts": __import__("time").time(),
    }
    fd, tmp_path = tempfile.mkstemp(prefix=".watchlist_subscribe.", dir=_CACHE_DIR)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(body, f)
        try:
            os.rename(tmp_path, _OUT_FILE)
        except OSError:
            # Windows: rename fails if destination exists — replace explicitly
            os.replace(tmp_path, _OUT_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


async def _loop() -> None:
    global _running
    _running = True
    while _running:
        try:
            pairs = await asyncio.to_thread(_collect)
            await asyncio.to_thread(_write_atomic, pairs)
        except Exception as exc:
            logger.warning("watchlist dump failed: %s", exc)
        try:
            await asyncio.sleep(_INTERVAL_S)
        except asyncio.CancelledError:
            break
    _running = False


def start_watchlist_dump() -> None:
    """Kick off the periodic dump. Idempotent — safe to call from each
    web replica's lifespan; an extra writer is harmless because
    _write_atomic is, well, atomic."""
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop())


def stop_watchlist_dump() -> None:
    global _running, _task
    _running = False
    if _task:
        _task.cancel()
        _task = None
