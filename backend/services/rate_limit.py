"""Generic rate-limiter for non-auth endpoints.

Sister module to the inline limiter in `api/v1/auth.py`. Two backends:

  · **Redis** — primary. Survives restart, shared across the 2 app
    replicas (without it the user could just round-robin between
    upstreams to skirt the cap). Sliding-window via SETEX + INCR on
    a per-(bucket, key) hash: `rl:<bucket>:<key>:<window-floor>`.
    Auto-expires after `window_sec` seconds.

  · **In-memory** — fallback when Redis is unreachable. Same dict +
    lock pattern as before so we never fail-open on a Redis blip.

Buckets keep their own thresholds — payments-checkout is allowed less
frequently than promo-validate, both well below auth.
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
    # Public screener feeds (/funding, /long-short, /spot-short, /dex-short,
    # /all-arbitrage). Frontend uses WS for live updates; REST is cold-start
    # + intermittent poll — 120/min/IP is generous for legit traffic. Bots
    # blasting the same IP at 1000+ req/sec get 429 without us hitting the
    # arb _http pool. Cloudflare Free covers L3/L4; this covers L7.
    "screener_public":   _Bucket(max_attempts=120, window_sec=60, block_sec=120),
}


def client_key(request: Request, *, suffix: str | None = None) -> str:
    """Compose the rate-limit key. Prefers X-Forwarded-For (we always sit
    behind nginx) so two users behind the same machine don't collide."""
    fwd = request.headers.get("X-Forwarded-For", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
    if suffix:
        return f"{ip}:{suffix}"
    return ip


# ── Redis-backed counters (primary path) ──────────────────────────────────────
import os as _os

_REDIS_URL = _os.environ.get("REDIS_URL") or ""
_redis_client = None
_redis_failed_ts: float = 0.0
_REDIS_BACKOFF_S = 10.0


def _get_redis():
    """Lazy Redis client. Falls back to None (→ in-memory) when the
    server is down + recently failed."""
    global _redis_client, _redis_failed_ts
    if not _REDIS_URL:
        return None
    if _redis_client is not None:
        return _redis_client
    if time.time() - _redis_failed_ts < _REDIS_BACKOFF_S:
        return None
    try:
        import redis
        _redis_client = redis.from_url(
            _REDIS_URL,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            health_check_interval=30,
        )
        _redis_client.ping()
        return _redis_client
    except Exception as exc:
        _redis_client = None
        _redis_failed_ts = time.time()
        logger.warning("rate_limit redis connect failed: %s — falling back in-mem", exc)
        return None


def _redis_check_and_record(bucket: str, b: _Bucket, key: str) -> None:
    """Atomic INCR + EXPIRE on Redis. Raises 429 when over threshold."""
    r = _get_redis()
    if r is None:
        b.check(key)
        b.record(key)
        return
    # Window-floor key keeps each request into its own bucket so we get
    # a true sliding window without LUA. After window_sec the key
    # auto-expires.
    floor = int(time.time() // max(1, b.window_sec)) * b.window_sec
    redis_key = f"rl:{bucket}:{key}:{floor}"
    try:
        pipe = r.pipeline(transaction=False)
        pipe.incr(redis_key, 1)
        pipe.expire(redis_key, b.window_sec + 5)  # +5 s grace for clock skew
        count, _ = pipe.execute()
    except Exception as exc:
        logger.warning("rate_limit redis op failed (%s) — fallback in-mem", exc)
        b.check(key)
        b.record(key)
        return
    if int(count) > b.max_attempts:
        logger.warning(
            "rate-limit hit (redis): bucket=%s key=%s count=%d window=%ds",
            bucket, key, count, b.window_sec,
        )
        raise HTTPException(
            status_code=429,
            detail="Too many requests — slow down and try again in a minute.",
            headers={"Retry-After": str(b.block_sec)},
        )


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
    _redis_check_and_record(bucket, b, key)
