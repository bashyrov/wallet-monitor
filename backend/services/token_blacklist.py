"""JWT revocation list backed by Redis.

Why: long-lived tokens (30 days) outlive the user's intent — a logout
or admin block should immediately kill the session. Without a
blacklist, a stolen token works for the rest of its TTL.

Storage: Redis SET `auth:revoked:<jti>` with TTL = remaining token
lifetime. We compare incoming token's `jti` against the set on every
authenticated request via `is_revoked()` from `deps.get_current_user`.

Falls open (returns False) when Redis is unreachable so the auth path
doesn't lock everyone out during a Redis incident — paired with
operational alerting on Redis health.
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("avalant.token_blacklist")

_REDIS_URL = os.environ.get("REDIS_URL") or ""
_KEY_PREFIX = "auth:revoked:"
_client = None
_last_failure = 0.0
_BACKOFF = 10.0


def _get_client():
    global _client, _last_failure
    if not _REDIS_URL:
        return None
    if _client is not None:
        return _client
    if time.time() - _last_failure < _BACKOFF:
        return None
    try:
        import redis
        _client = redis.from_url(
            _REDIS_URL,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
            decode_responses=False,
        )
        _client.ping()
        return _client
    except Exception as exc:
        _client = None
        _last_failure = time.time()
        logger.warning("token_blacklist Redis connect failed: %s", exc)
        return None


def revoke(jti: str, ttl_seconds: int) -> bool:
    """Mark a JWT id as revoked for the remaining lifetime.
    `ttl_seconds` should be `exp - now` so the entry self-expires when
    the token would have been worthless anyway."""
    if not jti or ttl_seconds <= 0:
        return False
    c = _get_client()
    if c is None:
        return False
    try:
        c.setex(_KEY_PREFIX + jti, ttl_seconds, b"1")
        return True
    except Exception as exc:
        logger.warning("token_blacklist revoke failed: %s", exc)
        return False


def is_revoked(jti: str) -> bool:
    if not jti:
        return False
    c = _get_client()
    if c is None:
        # Fail open — if Redis is down we still let valid JWTs work
        # rather than locking out the whole user base.
        return False
    try:
        return c.exists(_KEY_PREFIX + jti) > 0
    except Exception:
        return False
