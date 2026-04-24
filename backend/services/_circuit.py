"""Per-exchange REST circuit breaker.

Bursts of 5xx from one venue (typical: OKX flapping, Aster rate-limited,
MEXC under maintenance) used to hammer our HTTP client with retries and
slow down every other cycle that shared the pool. Circuit breaker tracks
the last N failures per exchange; once the window threshold is hit the
venue is marked "open" for a cooldown period and callers skip it instead
of piling onto the dead endpoint.

Usage:
    from backend.services._circuit import circuit
    if not circuit.allow(exchange):
        return []  # skip this tick for this venue
    try:
        r = await fetch(...)
        circuit.ok(exchange)
    except Exception:
        circuit.fail(exchange)
        raise

Thread-safe: backed by a lock; per-exchange dict reads are atomic under
the GIL but failure accounting needs consistent state.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Deque

logger = logging.getLogger("avalant.circuit")


class _CircuitBreaker:
    # Defaults tuned for our ~0.5-2s REST cycles — a venue needs to be
    # genuinely flaky (10 failures in 60s) before we cut it off for a minute.
    def __init__(self, threshold: int = 10, window_s: float = 60.0,
                 cooldown_s: float = 60.0) -> None:
        self.threshold = threshold
        self.window_s = window_s
        self.cooldown_s = cooldown_s
        self._failures: dict[str, Deque[float]] = {}
        self._open_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, exchange: str) -> bool:
        """Return True if the caller should proceed with this exchange."""
        now = time.time()
        open_until = self._open_until.get(exchange, 0.0)
        if now < open_until:
            return False
        if open_until and now >= open_until:
            # Cooldown window ended — reset state
            with self._lock:
                self._open_until.pop(exchange, None)
                self._failures.pop(exchange, None)
            logger.info("circuit: %s — cooldown ended, retrying", exchange)
        return True

    def fail(self, exchange: str) -> None:
        now = time.time()
        with self._lock:
            q = self._failures.setdefault(exchange, deque())
            q.append(now)
            # Drop failures older than the window
            cutoff = now - self.window_s
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.threshold:
                self._open_until[exchange] = now + self.cooldown_s
                q.clear()
                logger.warning(
                    "circuit: %s OPEN — %d failures in %.0fs, cooling %ds",
                    exchange, self.threshold, self.window_s, self.cooldown_s,
                )

    def hard_fail(self, exchange: str, cooldown_s: float | None = None) -> None:
        """Immediately open the circuit for an explicit cooldown. Use for
        unambiguous bans (HTTP 418) or aggressive rate limits (HTTP 429)
        where one hit is enough signal — waiting for the threshold to
        accumulate just wastes requests we already know will fail.
        """
        now = time.time()
        cd = cooldown_s if cooldown_s is not None else self.cooldown_s
        with self._lock:
            self._open_until[exchange] = now + cd
            self._failures.pop(exchange, None)
        logger.warning("circuit: %s HARD-OPEN for %.0fs", exchange, cd)

    def ok(self, exchange: str) -> None:
        # A success halves the remembered failure count — so a transient
        # 5xx doesn't compound forever, but a genuinely flapping venue
        # still trips quickly on its next couple of hits.
        with self._lock:
            q = self._failures.get(exchange)
            if q:
                for _ in range(len(q) // 2):
                    q.popleft()

    def state(self) -> dict[str, dict]:
        """Snapshot for observability — used by /api/health/feeds."""
        now = time.time()
        out: dict[str, dict] = {}
        for ex, q in list(self._failures.items()):
            cutoff = now - self.window_s
            recent = [t for t in q if t >= cutoff]
            out[ex] = {"failures_window": len(recent)}
        for ex, until in list(self._open_until.items()):
            out.setdefault(ex, {})["open"] = True
            out[ex]["cooldown_left_s"] = max(0, round(until - now, 1))
        return out


# Module-level singleton — every service that wraps a REST fetch uses this
# one instance so circuit state is shared.
circuit = _CircuitBreaker()
