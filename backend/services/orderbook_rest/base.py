"""Pure-thread REST-backstop base class for orderbook streaming.

Why pure thread (not asyncio.to_thread):
  · At prod volumes the fetcher event loop is saturated by 11 WS adapters +
    prewarm + refresh loop. Going through `loop.run_in_executor` adds 3-6s
    tail latency because future resolution waits for the loop to schedule
    the callback. Pure threading.Thread bypasses the loop entirely and
    runs at ~stdlib overhead.
  · Orderbook WS adapters regularly hit 1011 keepalive timeouts (every few
    minutes under load). Without a REST backstop, arb pairs for that
    exchange disappear until WS reconnects. With a REST backstop writing
    to the same _book_cache, the pair stays visible at REST cadence (~1s).
  · GIL-atomic dict assignment lets the thread write _book_cache[key] =
    entry without any lock, safe wrt the WS task that also writes the same
    dict key from the event loop thread.
"""
from __future__ import annotations

import concurrent.futures as _cf
import logging
import random
import threading
import time
from abc import abstractmethod

import httpx

logger = logging.getLogger("avalant.orderbook.rest")


# Shared sync client — dedicated pool so orderbook REST doesn't compete with
# the async `_arb_http` in orderbook_cache.py or the funding REST pool.
_rest_http = httpx.Client(
    timeout=httpx.Timeout(connect=3.0, read=4.0, write=3.0, pool=1.0),
    headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip, deflate"},
    follow_redirects=True,
    limits=httpx.Limits(max_connections=120, max_keepalive_connections=40, keepalive_expiry=30),
    http2=False,
)


class OrderbookRestBackstop:
    """One instance per exchange. Polls top-N symbols every `interval_s`,
    writes directly into `orderbook_cache._book_cache`.

    Subclasses override `fetch_sync(symbol)` — everything else is shared.
    """

    name: str = ""
    # Wall-clock target per tick for the full symbol set. Symbols fetch in
    # parallel via ThreadPoolExecutor, so the ceiling is roughly
    # ceil(N/concurrency) * single_fetch_latency.
    interval_s: float = 1.0
    # HTTP workers per adapter. Most exchanges tolerate 8-10 concurrent
    # orderbook requests; tune per-exchange if upstream throttles.
    concurrency: int = 8
    # Cap for per-symbol request latency. Beyond this, abandon the fetch
    # so one slow symbol doesn't eat the whole tick budget.
    per_request_timeout_s: float = 3.5

    def __init__(self):
        self._symbols: set[str] = set()
        self._stop = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_tick_ts: float = 0.0
        self._last_tick_dur: float = 0.0
        self._last_ok: int = 0
        self._last_fail: int = 0
        self._fail_streak: int = 0
        self._pool: _cf.ThreadPoolExecutor | None = None

    # ── Public API ──────────────────────────────────────────────────────────

    def set_symbols(self, symbols: list[str]) -> None:
        with self._lock:
            self._symbols = {s.upper() for s in symbols if s}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._pool = _cf.ThreadPoolExecutor(
            max_workers=self.concurrency,
            thread_name_prefix=f"ob_rest_{self.name}_w",
        )
        self._thread = threading.Thread(
            target=self._loop,
            name=f"ob_rest_{self.name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def health(self) -> dict:
        age = time.time() - self._last_tick_ts if self._last_tick_ts else None
        return {
            "name":        self.name,
            "symbols":     len(self._symbols),
            "last_age_s":  None if age is None else round(age, 1),
            "last_dur_s":  round(self._last_tick_dur, 2),
            "last_ok":     self._last_ok,
            "last_fail":   self._last_fail,
            "fail_streak": self._fail_streak,
        }

    # ── To override ─────────────────────────────────────────────────────────

    @abstractmethod
    def fetch_sync(self, symbol: str) -> dict | None:
        """Blocking fetch — returns {"bids": [[price, size], ...], "asks": [...]}
        or None on any failure. Runs in a worker thread, so synchronous IO is
        fine. Never raises — return None instead.
        """
        raise NotImplementedError

    # ── Internals ──────────────────────────────────────────────────────────

    def _write_book(self, symbol: str, data: dict) -> None:
        from backend.services.orderbook_cache import _book_cache
        key = f"{self.name}:{symbol}"
        now = time.time()
        entry = _book_cache.setdefault(key, {})
        entry["data"] = data
        entry["ts"] = now
        entry["source"] = "rest_backstop"

    def _fetch_one(self, symbol: str) -> bool:
        try:
            data = self.fetch_sync(symbol)
        except Exception as exc:
            logger.debug("%s REST fetch %s failed: %s", self.name, symbol, exc)
            return False
        if not data:
            return False
        if not (data.get("bids") or data.get("asks")):
            return False
        self._write_book(symbol, data)
        return True

    def _loop(self) -> None:
        # Stagger initial tick so 11 adapters don't hammer all upstreams in
        # the same 10ms window.
        time.sleep(random.uniform(0.0, self.interval_s))
        logger.info(
            "orderbook REST backstop %s started (interval=%.1fs, concurrency=%d)",
            self.name, self.interval_s, self.concurrency,
        )
        while not self._stop:
            started = time.time()
            with self._lock:
                symbols = list(self._symbols)
            pool = self._pool
            if not symbols or pool is None:
                time.sleep(self.interval_s)
                continue

            futures = [pool.submit(self._fetch_one, s) for s in symbols]
            n_ok = 0
            n_fail = 0
            try:
                for fut in _cf.as_completed(futures, timeout=self.interval_s * 2.5):
                    try:
                        if fut.result(timeout=0):
                            n_ok += 1
                        else:
                            n_fail += 1
                    except Exception:
                        n_fail += 1
            except _cf.TimeoutError:
                # One or more futures ran past the per-tick budget. Leave them
                # in flight — ThreadPoolExecutor will reclaim threads as they
                # finish. Next tick starts anyway to keep cadence predictable.
                for fut in futures:
                    if fut.done():
                        try:
                            if fut.result(timeout=0):
                                n_ok += 1
                            else:
                                n_fail += 1
                        except Exception:
                            n_fail += 1

            self._last_ok = n_ok
            self._last_fail = n_fail
            self._last_tick_dur = time.time() - started
            if n_ok > 0:
                self._last_tick_ts = time.time()
                self._fail_streak = 0
            else:
                self._fail_streak += 1

            # Throttle error-log spam but keep visibility on ongoing outages.
            if self._fail_streak in (1, 5, 20, 60):
                logger.warning(
                    "%s REST backstop: %d consecutive all-fail ticks",
                    self.name, self._fail_streak,
                )

            slack = self.interval_s - self._last_tick_dur
            if slack > 0:
                time.sleep(slack)
