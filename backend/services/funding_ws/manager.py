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
import threading
import time

from .adapters import ADAPTERS
from .base import FundingWSAdapter

logger = logging.getLogger("avalant.funding_ws")


class FundingWSManager:
    def __init__(self):
        self._adapters: dict[str, FundingWSAdapter] = {}

    def _update_cb(self, exchange: str, symbol: str, row: dict) -> None:
        """Currently a no-op — adapters manage their own _rows dict. Kept as
        the hook surface in case we later want to fan out to Redis / pub-sub.
        """
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
        # Filter to usable rows only (price set, rate non-None).
        complete = [
            r for r in a.rows()
            if r.get("price") and r.get("rate") is not None
        ]
        # Don't gate by aggregate `healthy` flag — it depends on
        # `_last_update_ts` which some adapters (notably Hyperliquid
        # whose WS only emits per-subscription and whose REST backstop
        # writes might not trigger the aggregate timestamp reliably)
        # leave stale even though individual rows are fresh. Per-row
        # staleness is already enforced by `a.rows()` via row_stale_after_s.
        if not complete:
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
_dump_thread: threading.Thread | None = None
_dump_stop_evt: threading.Event | None = None

_CACHE_DIR  = "/tmp/avalant_cache"
_DUMP_FILE  = os.path.join(_CACHE_DIR, "funding_ws.json")
# Dump cadence — every 500ms. The file is ~300-700KB atomically renamed,
# costs <1ms of wall time on a local tmpfs and a fraction of a percent of
# a CPU core. Halving it from 2s to 0.5s shaves ~1.5s off the end-to-end
# WS-tick → browser latency for no practical cost.
_DUMP_EVERY = 0.5
_STALE_MAX  = 30.0             # file data older than this is ignored
_LOCK_FILE  = "/tmp/avalant_funding_ws.lock"


def _dump_loop_sync(mgr: FundingWSManager, stop_evt: "threading.Event") -> None:
    """Pure-thread dump loop — runs independent of the event loop.

    Previously this was an asyncio task; under heavy fetcher load (spot/dex
    compute gathers saturating the loop) `await asyncio.sleep(0.5)` stretched
    to 15-20s, which surfaced on the web role as "all exchanges same age_s =
    file-mtime" because funding_ws.json wasn't rewritten. A dedicated thread
    bypasses the loop and guarantees the 0.5s cadence.
    """
    import threading  # local import keeps module import cheap
    os.makedirs(_CACHE_DIR, exist_ok=True)
    while not stop_evt.is_set():
        try:
            snapshot = {ex: a.rows() for ex, a in mgr._adapters.items() if a.rows()}
            if snapshot:
                ts_by_ex = {
                    ex: a._last_update_ts
                    for ex, a in mgr._adapters.items()
                    if a._last_update_ts
                }
                body = {"ts": time.time(), "rows": snapshot, "ts_by_ex": ts_by_ex}
                fd, tmp = tempfile.mkstemp(dir=_CACHE_DIR, suffix=".tmp")
                with os.fdopen(fd, "w") as f:
                    json.dump(body, f)
                os.replace(tmp, _DUMP_FILE)
        except Exception as exc:
            logger.debug("funding_ws dump failed: %s", exc)
        stop_evt.wait(_DUMP_EVERY)


def start_funding_ws_manager() -> FundingWSManager | None:
    """Attempt to become the funding-WS owner. Only one worker wins the
    lock; losers become passive file readers — they don't open any WS
    connections, keeping per-container CPU flat.
    """
    global _manager, _owner_lock_fd, _dump_thread, _dump_stop_evt
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
    _dump_stop_evt = threading.Event()
    _dump_thread = threading.Thread(
        target=_dump_loop_sync,
        args=(_manager, _dump_stop_evt),
        name="funding-ws-dump",
        daemon=True,
    )
    _dump_thread.start()
    return _manager


def get_funding_ws_manager() -> FundingWSManager | None:
    return _manager


def stop_funding_ws_manager() -> None:
    global _manager, _dump_thread, _dump_stop_evt, _owner_lock_fd
    if _dump_stop_evt is not None:
        _dump_stop_evt.set()
    _dump_thread = None
    _dump_stop_evt = None
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
    # Keep only rows with price AND non-None rate. Do NOT apply the
    # 'ratio of complete > 25%' gate that used to live here: exchanges
    # like OKX split rate across a separate REST poll (not the main WS
    # broadcast), so most adapter rows have rate=None and the ratio
    # guard was dropping the entire exchange. We'd rather serve 20 usable
    # OKX symbols than silently return nothing because the ratio is low.
    complete = [r for r in rows if r.get("price") and r.get("rate") is not None]
    if not complete:
        return None
    return complete


def is_ws_funding_supported(exchange: str) -> bool:
    return exchange in ADAPTERS


def ws_health() -> dict[str, dict]:
    return _manager.health() if _manager else {}
