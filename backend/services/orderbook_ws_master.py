"""Fetcher split master: spawn + merger for per-exchange workers.

Architecture (see docs/fetcher-split-rfc.md, milestone M2):

    ┌────────────────────────────────────────────────────────┐
    │ fetcher (master, this module)                          │
    │  · spawn + health-check child workers                  │
    │  · merge books.<ex>.json → books.json (100ms cadence)  │
    │  · arb / spot / dex compute stays in-process           │
    └──┬────────────────────┬──────────────────┬──────────────┘
       │                    │                  │
    ┌──▼──┐             ┌───▼───┐         ┌────▼─────┐
    │bnc  │  ...        │bybit  │  ...    │kucoin    │
    │uvl. │             │uvl.   │         │uvl.      │
    │book │             │book.  │         │book.     │
    │.bnc.│             │bybit. │         │kucoin.   │
    │json │             │json   │         │json      │
    └─────┘             └───────┘         └──────────┘

Activated via env:
    AVALANT_FETCHER_MODE=multiprocess
    AVALANT_WORKER_EXCHANGES=binance,bybit,okx,gate,kucoin,mexc,bitget,aster,whitebit,bingx,hyperliquid

Default (unset / `single`) keeps the legacy single-process flow unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

logger = logging.getLogger("avalant.orderbook_master")

_CACHE_DIR = "/tmp/avalant_cache"
_BOOKS_FILE = os.path.join(_CACHE_DIR, "books.json")
_MERGE_INTERVAL_S = 0.2   # merge + write cadence
_STALE_SERVE_MAX_S = 30.0 # drop entries older than this from the merged file
_RESTART_COOLDOWN_S = 5.0 # wait this long before respawning a crashed worker
_MAX_RESTARTS_PER_MIN = 6


def worker_exchanges() -> list[str]:
    raw = (os.environ.get("AVALANT_WORKER_EXCHANGES") or "").strip()
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def is_multiprocess_mode() -> bool:
    return (os.environ.get("AVALANT_FETCHER_MODE") or "").strip().lower() == "multiprocess"


# ── Merger ──────────────────────────────────────────────────────────────────
def _merge_loop(stop_evt: threading.Event, owned: list[str]) -> None:
    """Every _MERGE_INTERVAL_S: read books.<ex>.json for each owned exchange,
    merge into one dict, write atomically to books.json.

    Cheap — each file is an in-memory dict; merge is O(sum(files)). Runs in a
    daemon thread, fully decoupled from any event loop."""
    logger.info("merger thread started (exchanges=%s)", owned)
    while not stop_evt.is_set():
        try:
            merged: dict[str, dict] = {}
            cutoff = time.time() - _STALE_SERVE_MAX_S
            for ex in owned:
                path = os.path.join(_CACHE_DIR, f"books.{ex}.json")
                try:
                    with open(path) as f:
                        data = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    continue
                if not isinstance(data, dict):
                    continue
                for key, entry in data.items():
                    ts = entry.get("ts", 0) if isinstance(entry, dict) else 0
                    if ts < cutoff:
                        continue
                    merged[key] = entry
            # Atomic rename keeps concurrent readers consistent.
            fd, tmp = tempfile.mkstemp(dir=_CACHE_DIR, prefix="books.", suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(merged, f, separators=(",", ":"))
            os.replace(tmp, _BOOKS_FILE)
        except Exception as exc:
            logger.warning("merger tick failed: %s", exc)
        stop_evt.wait(_MERGE_INTERVAL_S)


# ── Worker lifecycle ────────────────────────────────────────────────────────
class _WorkerProc:
    """Spawns and supervises a single `python -m backend.services.orderbook_ws_worker`
    subprocess for one exchange. Crash → log + restart with an exponential-ish
    cool-down capped at _RESTART_COOLDOWN_S × _MAX_RESTARTS_PER_MIN."""

    def __init__(self, exchange: str):
        self.exchange = exchange
        self._proc: subprocess.Popen | None = None
        self._restart_window: list[float] = []  # timestamps of recent starts
        self._stop = False

    def _spawn(self) -> None:
        env = os.environ.copy()
        env["AVALANT_OWNED_EXCHANGE"] = self.exchange
        # Children don't need (and mustn't try to start) the refresh / spot /
        # dex / alert loops — they only run the WS stream.
        env["AVALANT_ROLE"] = "fetcher-worker"
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "backend.services.orderbook_ws_worker"],
            env=env,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        self._restart_window.append(time.time())
        logger.info("spawned worker for %s (pid=%d)", self.exchange, self._proc.pid)

    def _too_many_restarts(self) -> bool:
        cutoff = time.time() - 60.0
        self._restart_window = [t for t in self._restart_window if t >= cutoff]
        return len(self._restart_window) > _MAX_RESTARTS_PER_MIN

    def supervise(self, stop_evt: threading.Event) -> None:
        """Loop: spawn → wait → respawn on exit. Runs in a daemon thread per
        exchange. Cooperative cancel via stop_evt."""
        while not stop_evt.is_set() and not self._stop:
            self._spawn()
            assert self._proc is not None
            while not stop_evt.is_set() and self._proc.poll() is None:
                if stop_evt.wait(0.5):
                    break
            if stop_evt.is_set() or self._stop:
                break
            rc = self._proc.returncode
            logger.warning("worker %s exited code=%s — will restart", self.exchange, rc)
            if self._too_many_restarts():
                logger.error(
                    "worker %s restarted too often (%d times in 60s) — backing off for 60s",
                    self.exchange, len(self._restart_window),
                )
                stop_evt.wait(60.0)
            else:
                stop_evt.wait(_RESTART_COOLDOWN_S)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop = True
        p = self._proc
        if not p:
            return
        if p.poll() is None:
            try:
                p.send_signal(signal.SIGTERM)
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()


_workers: list[_WorkerProc] = []
_supervisor_threads: list[threading.Thread] = []
_merge_thread: threading.Thread | None = None
_stop_evt: threading.Event | None = None


def start_workers_and_merger() -> None:
    """Spawn one worker per AVALANT_WORKER_EXCHANGES entry + start the merger.

    Idempotent: safe to call multiple times; second call is a no-op when
    workers are already up."""
    global _merge_thread, _stop_evt
    if _stop_evt is not None:
        return
    exchanges = worker_exchanges()
    if not exchanges:
        logger.error("AVALANT_FETCHER_MODE=multiprocess set but AVALANT_WORKER_EXCHANGES is empty")
        return

    os.makedirs(_CACHE_DIR, exist_ok=True)
    _stop_evt = threading.Event()

    for ex in exchanges:
        w = _WorkerProc(ex)
        t = threading.Thread(target=w.supervise, args=(_stop_evt,),
                             name=f"supervisor-{ex}", daemon=True)
        t.start()
        _workers.append(w)
        _supervisor_threads.append(t)

    _merge_thread = threading.Thread(
        target=_merge_loop, args=(_stop_evt, exchanges),
        name="orderbook-merger", daemon=True,
    )
    _merge_thread.start()
    logger.info("multiprocess fetcher up: %d worker(s), merger running", len(exchanges))


def stop_workers_and_merger(timeout: float = 5.0) -> None:
    global _stop_evt, _merge_thread
    if _stop_evt is None:
        return
    _stop_evt.set()
    for w in _workers:
        w.stop(timeout=timeout)
    _workers.clear()
    for t in _supervisor_threads:
        t.join(timeout=timeout)
    _supervisor_threads.clear()
    if _merge_thread and _merge_thread.is_alive():
        _merge_thread.join(timeout=timeout)
    _merge_thread = None
    _stop_evt = None
    logger.info("multiprocess fetcher stopped")
