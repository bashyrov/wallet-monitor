"""Funding WS manager — owned by a single uvicorn worker (file lock),
dumps rows to a shared JSON file every few seconds so every worker can
read them.

Usage from the rest of the codebase:

    from backend.services import funding_ws

    funding_ws.start_funding_ws_manager()          # app startup (safe on every worker)
    rows = funding_ws.get_ws_rows("binance")       # None if stale / not healthy
    ok   = funding_ws.is_ws_funding_supported("okx")
    funding_ws.stop_funding_ws_manager()           # app shutdown
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import tempfile
import time

from .adapters import ADAPTERS
from .base import FundingWSAdapter

logger = logging.getLogger("avalant.funding_ws")


class FundingWSManager:
    def __init__(self):
        self._adapters: dict[str, FundingWSAdapter] = {}

    def _update_cb(self, exchange: str, symbol: str, row: dict) -> None:
        """Fan out to the event-driven arb engine so every funding tick can
        trigger a per-symbol recompute. Adapter still owns its own _rows
        dict — we just notify the consumer about the change."""
        try:
            from backend.services.arbitrage_service import on_row_update
            on_row_update(exchange, symbol)
        except Exception:
            pass

    def start_all(self) -> None:
        for ex, cls in ADAPTERS.items():
            if ex not in self._adapters:
                a = cls(self._update_cb)
                self._adapters[ex] = a
                a.start()
        logger.info("funding WS manager started (%d adapters)", len(self._adapters))

    def stop_all(self) -> None:
        for a in self._adapters.values():
            a.stop()
        self._adapters.clear()

    def rows(self, exchange: str) -> list[dict] | None:
        a = self._adapters.get(exchange)
        if not a:
            return None
        if not a.health().get("healthy"):
            return None
        # Only return rows where the critical fields are populated — some
        # venues (KuCoin's funding.rate subject, BingX markPrice) don't
        # fire on every tick, so a "healthy" adapter might still be
        # missing funding rate on fresh symbols. In that case the caller
        # falls back to REST rather than serving a broken row.
        complete = [
            r for r in a.rows()
            if r.get("price") and r.get("rate") is not None
        ]
        if len(complete) < max(5, len(a.rows()) // 4):
            # Adapter running but not producing enough usable data
            return None
        return complete

    def raw_rows(self, exchange: str) -> list[dict] | None:
        """Rows regardless of health — useful for debug / admin metrics."""
        a = self._adapters.get(exchange)
        return a.rows() if a else None

    def health(self) -> dict[str, dict]:
        return {ex: a.health() for ex, a in self._adapters.items()}


_manager: FundingWSManager | None = None
_owner_lock_fd = None          # only the owner worker has the manager running
_dump_task: asyncio.Task | None = None

_CACHE_DIR  = "/tmp/avalant_cache"
_DUMP_FILE  = os.path.join(_CACHE_DIR, "funding_ws.json")
# Dump cadence — every 500ms. The file is ~300-700KB atomically renamed,
# costs <1ms of wall time on a local tmpfs and a fraction of a percent of
# a CPU core. Halving it from 2s to 0.5s shaves ~1.5s off the end-to-end
# WS-tick → browser latency for no practical cost.
_DUMP_EVERY = 0.5
_STALE_MAX  = 30.0             # file data older than this is ignored
_LOCK_FILE  = "/tmp/avalant_funding_ws.lock"


async def _dump_loop(mgr: FundingWSManager) -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    while True:
        try:
            snapshot = {ex: a.rows() for ex, a in mgr._adapters.items() if a.rows()}
            if snapshot:
                body = {"ts": time.time(), "rows": snapshot}
                fd, tmp = tempfile.mkstemp(dir=_CACHE_DIR, suffix=".tmp")
                with os.fdopen(fd, "w") as f:
                    json.dump(body, f)
                os.replace(tmp, _DUMP_FILE)
        except Exception as exc:
            logger.debug("funding_ws dump failed: %s", exc)
        await asyncio.sleep(_DUMP_EVERY)


def start_funding_ws_manager() -> FundingWSManager | None:
    """Attempt to become the funding-WS owner. Only one worker wins the
    lock; losers become passive file readers — they don't open any WS
    connections, keeping per-container CPU flat.
    """
    global _manager, _owner_lock_fd, _dump_task
    if _manager is not None:
        return _manager
    try:
        _owner_lock_fd = open(_LOCK_FILE, "w")
        fcntl.flock(_owner_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        logger.info("funding WS: another worker holds the lock — reader-only mode")
        return None
    _manager = FundingWSManager()
    _manager.start_all()
    try:
        loop = asyncio.get_event_loop()
        _dump_task = loop.create_task(_dump_loop(_manager))
    except RuntimeError:
        pass
    return _manager


def get_funding_ws_manager() -> FundingWSManager | None:
    return _manager


def stop_funding_ws_manager() -> None:
    global _manager, _dump_task, _owner_lock_fd
    if _dump_task and not _dump_task.done():
        _dump_task.cancel()
    _dump_task = None
    if _manager is not None:
        _manager.stop_all()
        _manager = None
    if _owner_lock_fd is not None:
        try:
            _owner_lock_fd.close()
        except Exception:
            pass
        _owner_lock_fd = None


# ── Shared-file reader (used by every worker) ─────────────────────────────────
_reader_memo: dict = {"ts": 0.0, "data": None}


def _read_dump() -> dict | None:
    """Load the owner's dump if fresh enough. Memoised for 250ms so a burst
    of _get_rows() calls doesn't thrash the disk, but the reader catches up
    to the 500ms dump cadence within one cycle."""
    now = time.time()
    if _reader_memo["data"] is not None and now - _reader_memo["ts"] < 0.25:
        return _reader_memo["data"]
    try:
        with open(_DUMP_FILE) as f:
            body = json.load(f)
        if not isinstance(body, dict):
            return None
        if now - (body.get("ts") or 0) > _STALE_MAX:
            return None
        _reader_memo["data"] = body
        _reader_memo["ts"] = now
        return body
    except (FileNotFoundError, ValueError):
        return None


def get_ws_rows(exchange: str) -> list[dict] | None:
    """Rows for an exchange. On the owner worker this reads the live
    in-memory manager; on every other worker it reads the shared dump.
    Returns None if nothing fresh is available — caller falls through to
    REST (via arbitrage_service._get_rows)."""
    if _manager is not None:
        return _manager.rows(exchange)
    dump = _read_dump()
    if not dump:
        return None
    rows = (dump.get("rows") or {}).get(exchange)
    if not rows:
        return None
    # Re-apply the completeness check — dump serialises raw adapter rows.
    complete = [r for r in rows if r.get("price") and r.get("rate") is not None]
    if len(complete) < max(5, len(rows) // 4):
        return None
    return complete


def is_ws_funding_supported(exchange: str) -> bool:
    return exchange in ADAPTERS


def ws_health() -> dict[str, dict]:
    return _manager.health() if _manager else {}
