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
_FUNDING_WS_FILE = os.path.join(_CACHE_DIR, "funding_ws.json")
_HEALTH_FILE = os.path.join(_CACHE_DIR, "fetcher_workers.json")
_MERGE_INTERVAL_S = 0.2   # orderbook merge + write cadence
_FUNDING_MERGE_INTERVAL_S = 0.5  # funding_ws merge + write cadence
_HEALTH_DUMP_INTERVAL_S = 5.0  # fetcher_workers.json refresh cadence
_STALE_SERVE_MAX_S = 30.0 # drop entries older than this from the merged file
_RESTART_COOLDOWN_S = 5.0 # wait this long before respawning a crashed worker
_MAX_RESTARTS_PER_MIN = 6


def worker_exchanges() -> list[str]:
    raw = (os.environ.get("AVALANT_WORKER_EXCHANGES") or "").strip()
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def funding_worker_exchanges() -> list[str]:
    """Same list semantics as worker_exchanges(), but for funding-WS workers."""
    raw = (os.environ.get("AVALANT_FUNDING_WORKER_EXCHANGES") or "").strip()
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def is_multiprocess_mode() -> bool:
    return (os.environ.get("AVALANT_FETCHER_MODE") or "").strip().lower() == "multiprocess"


# ── Merger ──────────────────────────────────────────────────────────────────
def _merge_loop(stop_evt: threading.Event, owned: list[str]) -> None:
    """Every _MERGE_INTERVAL_S: read books.<ex>.json for each owned exchange,
    merge into one dict, write atomically to books.json.

    Cheap — each file is an in-memory dict; merge is O(sum(files)). Runs in a
    daemon thread, fully decoupled from any event loop."""
    logger.info("orderbook merger thread started (exchanges=%s)", owned)
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
            logger.warning("orderbook merger tick failed: %s", exc)
        stop_evt.wait(_MERGE_INTERVAL_S)


def _funding_merge_loop(stop_evt: threading.Event, owned: list[str]) -> None:
    """Every _FUNDING_MERGE_INTERVAL_S: read funding_ws.<ex>.json per worker,
    merge `rows` and `ts_by_ex` into one body, write funding_ws.json atomically.

    Shape preserved from the single-process dumper:
        {ts, rows: {ex: [row, ...]}, ts_by_ex: {ex: last_update_ts}}
    """
    logger.info("funding merger thread started (exchanges=%s)", owned)
    while not stop_evt.is_set():
        try:
            merged_rows: dict[str, list] = {}
            merged_ts: dict[str, float] = {}
            for ex in owned:
                path = os.path.join(_CACHE_DIR, f"funding_ws.{ex}.json")
                try:
                    with open(path) as f:
                        data = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    continue
                if not isinstance(data, dict):
                    continue
                for inner_ex, rows in (data.get("rows") or {}).items():
                    if isinstance(rows, list):
                        merged_rows[inner_ex] = rows
                for inner_ex, ts in (data.get("ts_by_ex") or {}).items():
                    if isinstance(ts, (int, float)):
                        merged_ts[inner_ex] = ts
            if not merged_rows:
                stop_evt.wait(_FUNDING_MERGE_INTERVAL_S)
                continue
            body = {"ts": time.time(), "rows": merged_rows, "ts_by_ex": merged_ts}
            fd, tmp = tempfile.mkstemp(dir=_CACHE_DIR, prefix="fws.", suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(body, f, separators=(",", ":"))
            os.replace(tmp, _FUNDING_WS_FILE)
        except Exception as exc:
            logger.warning("funding merger tick failed: %s", exc)
        stop_evt.wait(_FUNDING_MERGE_INTERVAL_S)


# ── Worker lifecycle ────────────────────────────────────────────────────────
class _WorkerProc:
    """Spawns and supervises a single per-exchange worker subprocess.

    Two flavours, selected by `kind`:
      · "orderbook" → python -m backend.services.orderbook_ws_worker
                      (env: AVALANT_OWNED_EXCHANGE=<ex>)
      · "funding"   → python -m backend.services.funding_ws_worker
                      (env: AVALANT_OWNED_FUNDING_EXCHANGE=<ex>)

    Crash → log + restart with a cool-down capped at _RESTART_COOLDOWN_S ×
    _MAX_RESTARTS_PER_MIN (back-off of 60s when the cap trips)."""

    _MODULE_BY_KIND = {
        "orderbook": ("backend.services.orderbook_ws_worker", "AVALANT_OWNED_EXCHANGE"),
        "funding":   ("backend.services.funding_ws_worker",   "AVALANT_OWNED_FUNDING_EXCHANGE"),
    }

    def __init__(self, exchange: str, kind: str = "orderbook"):
        if kind not in self._MODULE_BY_KIND:
            raise ValueError(f"unknown worker kind: {kind}")
        self.exchange = exchange
        self.kind = kind
        self._proc: subprocess.Popen | None = None
        self._restart_window: list[float] = []
        self._started_at: float = 0.0
        self._exit_count: int = 0
        self._last_exit_rc: int | None = None
        self._stop = False

    def snapshot(self) -> dict:
        p = self._proc
        return {
            "kind": self.kind,
            "exchange": self.exchange,
            "pid": p.pid if p else None,
            "alive": bool(p and p.poll() is None),
            "started_at": self._started_at,
            "uptime_s": round(time.time() - self._started_at, 1) if self._started_at else 0,
            "restarts_1m": len([
                t for t in self._restart_window if t >= time.time() - 60.0
            ]),
            "exit_count": self._exit_count,
            "last_exit_rc": self._last_exit_rc,
        }

    def _spawn(self) -> None:
        module, env_key = self._MODULE_BY_KIND[self.kind]
        env = os.environ.copy()
        env[env_key] = self.exchange
        env["AVALANT_ROLE"] = "fetcher-worker"
        self._proc = subprocess.Popen(
            [sys.executable, "-m", module],
            env=env,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        now = time.time()
        self._restart_window.append(now)
        self._started_at = now
        logger.info("spawned %s worker for %s (pid=%d)", self.kind, self.exchange, self._proc.pid)

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
            self._exit_count += 1
            self._last_exit_rc = rc
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
_funding_merge_thread: threading.Thread | None = None
_health_thread: threading.Thread | None = None
_stop_evt: threading.Event | None = None


def _health_dump_loop(stop_evt: threading.Event) -> None:
    """Every _HEALTH_DUMP_INTERVAL_S write fetcher_workers.json so the web
    role can surface /api/health/fetcher without IPC. Small — 11 entries,
    a few hundred bytes total."""
    logger.info("health dump thread started")
    while not stop_evt.is_set():
        try:
            snap = {
                "ts": time.time(),
                "workers": [w.snapshot() for w in _workers],
            }
            fd, tmp = tempfile.mkstemp(dir=_CACHE_DIR, prefix="fw.", suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(snap, f, separators=(",", ":"))
            os.replace(tmp, _HEALTH_FILE)
        except Exception as exc:
            logger.debug("health dump failed: %s", exc)
        stop_evt.wait(_HEALTH_DUMP_INTERVAL_S)


def read_workers_health() -> dict:
    """Web-role reader. Never raises — returns empty dict on any issue so
    the /api/health/fetcher endpoint degrades gracefully."""
    try:
        with open(_HEALTH_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def start_workers_and_merger() -> None:
    """Spawn one worker per AVALANT_WORKER_EXCHANGES entry + start the merger.

    Idempotent: safe to call multiple times; second call is a no-op when
    workers are already up."""
    global _merge_thread, _funding_merge_thread, _stop_evt
    if _stop_evt is not None:
        return
    ob_exchanges = worker_exchanges()
    fn_exchanges = funding_worker_exchanges()
    if not ob_exchanges and not fn_exchanges:
        logger.error(
            "AVALANT_FETCHER_MODE=multiprocess set but both AVALANT_WORKER_EXCHANGES "
            "and AVALANT_FUNDING_WORKER_EXCHANGES are empty"
        )
        return

    os.makedirs(_CACHE_DIR, exist_ok=True)
    _stop_evt = threading.Event()

    for ex in ob_exchanges:
        w = _WorkerProc(ex, kind="orderbook")
        t = threading.Thread(target=w.supervise, args=(_stop_evt,),
                             name=f"supervisor-ob-{ex}", daemon=True)
        t.start()
        _workers.append(w)
        _supervisor_threads.append(t)

    for ex in fn_exchanges:
        w = _WorkerProc(ex, kind="funding")
        t = threading.Thread(target=w.supervise, args=(_stop_evt,),
                             name=f"supervisor-fn-{ex}", daemon=True)
        t.start()
        _workers.append(w)
        _supervisor_threads.append(t)

    if ob_exchanges:
        _merge_thread = threading.Thread(
            target=_merge_loop, args=(_stop_evt, ob_exchanges),
            name="orderbook-merger", daemon=True,
        )
        _merge_thread.start()

    if fn_exchanges:
        _funding_merge_thread = threading.Thread(
            target=_funding_merge_loop, args=(_stop_evt, fn_exchanges),
            name="funding-merger", daemon=True,
        )
        _funding_merge_thread.start()

    global _health_thread
    _health_thread = threading.Thread(
        target=_health_dump_loop, args=(_stop_evt,),
        name="orderbook-health", daemon=True,
    )
    _health_thread.start()

    logger.info("multiprocess fetcher up: ob=%d fn=%d worker(s), mergers + health running",
                len(ob_exchanges), len(fn_exchanges))


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
    global _funding_merge_thread, _health_thread
    if _funding_merge_thread and _funding_merge_thread.is_alive():
        _funding_merge_thread.join(timeout=timeout)
    _funding_merge_thread = None
    if _health_thread and _health_thread.is_alive():
        _health_thread.join(timeout=timeout)
    _health_thread = None
    _stop_evt = None
    logger.info("multiprocess fetcher stopped")
