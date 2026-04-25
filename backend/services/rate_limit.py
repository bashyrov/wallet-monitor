"""Generic rate-limiter for non-auth endpoints.

Sister module to the inline limiter in `api/v1/auth.py` — that one stays
tied to login/register state, this one is reusable across services. Per
(bucket, ip) sliding-window counter, in-memory only (single-process app
container), with a clear `RateLimited` exception on overflow that the
FastAPI handler converts to 429.

Buckets keep their own thresholds — payments-checkout is allowed less
frequently than promo-validate, both well below auth.

Move to Redis-backed counters when we scale past a single uvicorn
process. The dict + lock approach is fine for the current 1× app
container and survives a restart by intentionally resetting (no
persisted hostility).
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from threading import Lock

from fastapi import HTTPException, Request

logger = logging.getLogger("avalant.rate_limit")


class _Bucket:
    __slots__ = ("max_attempts", "window_sec", "block_sec", "_attempts", "_lock")

    def __init__(self, *, max_attempts: int, window_sec: int, block_sec: int):
        self.max_attempts = max_attempts
        self.window_sec = window_sec
        self.block_sec = block_sec
        self._attempts: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            ts = self._attempts[key]
            # Drop expired entries.
            cutoff = now - self.window_sec
            self._attempts[key] = [t for t in ts if t > cutoff]
            if len(self._attempts[key]) >= self.max_attempts:
                logger.warning(
                    "rate-limit hit: key=%s count=%d window=%ds",
                    key, len(self._attempts[key]), self.window_sec,
                )
                raise HTTPException(
                    status_code=429,
                    detail="Too many requests — slow down and try again in a minute.",
                    headers={"Retry-After": str(self.block_sec)},
                )

    def record(self, key: str) -> None:
        with self._lock:
            self._attempts[key].append(time.monotonic())


# Per-bucket presets. Tunable from one place; admins eventually configurable.
_BUCKETS: dict[str, _Bucket] = {
    # Checkout creation hits CryptoCloud's API — keep this conservative.
    "payments_checkout": _Bucket(max_attempts=5,  window_sec=60,  block_sec=300),
    # Promo validation is read-only but a popular abuse vector for code
    # enumeration; treat it like a brute-force surface.
    "promo_validate":    _Bucket(max_attempts=10, window_sec=60,  block_sec=300),
    # Wallet creation triggers an exchange-side validate_key() call which
    # costs us money on rate-limited venues. 30/h is plenty for legit use.
    "wallets_create":    _Bucket(max_attempts=30, window_sec=3600, block_sec=900),
    # Generic admin write actions — guard against an admin-key leak that
    # tries to mass-mutate plans / promos.
    "admin_write":       _Bucket(max_attempts=60, window_sec=60,  block_sec=120),
}


def client_key(request: Request, *, suffix: str | None = None) -> str:
    """Compose the rate-limit key. Prefers X-Forwarded-For (we always sit
    behind nginx) so two users behind the same machine don't collide."""
    fwd = request.headers.get("X-Forwarded-For", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
    if suffix:
        return f"{ip}:{suffix}"
    return ip


def enforce(bucket: str, request: Request, *, suffix: str | None = None) -> None:
    """Single call site for every protected endpoint.

    Raises 429 immediately when the bucket is full, otherwise records the
    attempt and returns. Use this BEFORE any expensive call (HTTP to
    exchange / DB write) so legitimate traffic isn't billed for an abuser.
    """
    b = _BUCKETS.get(bucket)
    if b is None:
        # Unknown bucket — fail open to avoid breaking endpoints during a
        # rollout where the new service code hits an old config. Logged
        # so it surfaces on the first deploy.
        logger.warning("rate_limit.enforce called with unknown bucket %r", bucket)
        return
    key = client_key(request, suffix=suffix)
    b.check(key)
    b.record(key)
