"""Redis-backed cache for authenticated user lookups.

Every request to a protected endpoint used to cost a JWT decode + one
`User` SELECT (is_blocked check). With 500 concurrent users and ~10 req/s
each, that's ~5000 Postgres round-trips/s. Redis cuts the round-trip by
60× (sub-ms local-network lookup vs 10-30ms through pgbouncer + Postgres).

Design:
  - Key:  `auth:<token_hash>` — first 32 chars of the JWT are enough to
          deduplicate tokens without holding the full token in Redis memory.
  - Value: `<user_id>:<is_blocked_bit>:<is_admin_bit>` (compact string).
  - TTL:  60s — keeps block/unblock reasonably snappy while absorbing the
          bulk of repeat calls from a single /arb tab or the arb WS poll.

Graceful degradation: if Redis is unreachable, the cache is a no-op —
callers still work via the DB path. We never raise from this module.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("avalant.auth_cache")

_REDIS_URL = os.environ.get("REDIS_URL") or ""
_TTL_S = 60
_client = None
_client_last_failure_ts: float = 0.0
_CLIENT_BACKOFF_S = 10.0  # after a connect failure, wait before retrying


def _get_client():
    """Lazy-init Redis client. None if REDIS_URL unset or recent failure."""
    global _client, _client_last_failure_ts
    if not _REDIS_URL:
        return None
    if _client is not None:
        return _client
    if time.time() - _client_last_failure_ts < _CLIENT_BACKOFF_S:
        return None
    try:
        import redis  # imported lazily so app can boot without the package
        _client = redis.from_url(
            _REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
            health_check_interval=30,
        )
        # Ping once so we fail fast on misconfig rather than on every
        # read/write attempt.
        _client.ping()
        logger.info("auth_cache: connected to %s", _REDIS_URL)
        return _client
    except Exception as exc:
        _client = None
        _client_last_failure_ts = time.time()
        logger.warning("auth_cache: connect failed (%s) — falling back to DB", exc)
        return None


def _hash_token(token: str) -> str:
    """Hash the token to keep Redis values fixed-length and avoid storing raw
    JWTs. 16 bytes of SHA-256 hex = 32 chars, collision-free for our scale."""
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def get_cached_user(token: str) -> Optional[tuple[int, bool, bool]]:
    """Returns (user_id, is_blocked, is_admin) or None if not cached/down."""
    c = _get_client()
    if c is None or not token:
        return None
    try:
        v = c.get(f"auth:{_hash_token(token)}")
        if not v:
            return None
        parts = v.split(":")
        if len(parts) != 3:
            return None
        return int(parts[0]), parts[1] == "1", parts[2] == "1"
    except Exception:
        return None


def cache_user(token: str, user_id: int, is_blocked: bool, is_admin: bool) -> None:
    c = _get_client()
    if c is None or not token:
        return
    try:
        c.setex(
            f"auth:{_hash_token(token)}",
            _TTL_S,
            f"{user_id}:{'1' if is_blocked else '0'}:{'1' if is_admin else '0'}",
        )
    except Exception:
        pass


def invalidate_user(user_id: int) -> None:
    """Called on user-state changes (block/unblock, plan change) so we don't
    keep serving stale privileges. Scans only the `auth:*` namespace."""
    c = _get_client()
    if c is None:
        return
    try:
        cursor = 0
        target_prefix = f"{user_id}:"
        while True:
            cursor, keys = c.scan(cursor=cursor, match="auth:*", count=500)
            if keys:
                vals = c.mget(keys)
                drop = [k for k, v in zip(keys, vals) if v and v.startswith(target_prefix)]
                if drop:
                    c.delete(*drop)
            if cursor == 0:
                break
    except Exception:
        pass
