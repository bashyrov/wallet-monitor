"""User online-presence tracking via Redis.

Web replicas bump a per-user heartbeat key on every authenticated API
request (via get_current_user). The fetcher's user-stream supervisor
reads this set every 60s and only opens WebSocket streams for users
whose heartbeat is fresh.

Lifecycle:
  · User logs in / opens any page  → /api/auth/me etc. → bump TTL
  · Browser keeps making requests (WS funding, polls, …) → keep TTL fresh
  · User closes tab / network drops / session expires → no more bumps
  · After ONLINE_TTL_S seconds (5 min by default) the key expires
  · Next supervisor scan stops the user's WS streams

If REDIS_URL isn't set, presence is unknown — supervisor falls back to
"all wallets are online" so we don't accidentally disable streams in
single-replica dev environments.
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("avalant.online")

ONLINE_TTL_S = 300  # 5 min — covers brief tab-switching / mobile sleep
_KEY_PREFIX = "avalant:online:"

_redis_client = None
_redis_last_failure: float = 0.0
_REDIS_BACKOFF_S = 10.0


def _redis():
    global _redis_client, _redis_last_failure
    url = os.environ.get("REDIS_URL") or ""
    if not url:
        return None
    if _redis_client is not None:
        return _redis_client
    if time.time() - _redis_last_failure < _REDIS_BACKOFF_S:
        return None
    try:
        import redis
        c = redis.from_url(url, decode_responses=True,
                           socket_connect_timeout=1.0, socket_timeout=1.0)
        c.ping()
        _redis_client = c
        return c
    except Exception as exc:
        _redis_last_failure = time.time()
        logger.debug("online_presence redis connect failed: %s", exc)
        return None


def heartbeat(user_id: int) -> None:
    """Bump the user's online TTL. Called from get_current_user on every
    authenticated request — that gives us 'logged-in AND making requests
    in the last 5 min' as the definition of online."""
    c = _redis()
    if c is None:
        return
    try:
        c.setex(f"{_KEY_PREFIX}{user_id}", ONLINE_TTL_S, str(int(time.time())))
    except Exception as exc:
        logger.debug("online_presence heartbeat failed user=%s: %s", user_id, exc)


def is_online(user_id: int) -> bool:
    """True if the user has hit any authenticated endpoint within
    ONLINE_TTL_S. Returns True (fail-open) when Redis is unavailable —
    streams in dev shouldn't break just because Redis isn't running."""
    c = _redis()
    if c is None:
        return True
    try:
        return c.exists(f"{_KEY_PREFIX}{user_id}") == 1
    except Exception:
        return True


def online_user_ids() -> set[int] | None:
    """Returns the set of currently-online user_ids, or None if Redis
    isn't available (caller should treat None as 'unknown — skip the
    online filter')."""
    c = _redis()
    if c is None:
        return None
    try:
        keys = c.keys(f"{_KEY_PREFIX}*")
        out: set[int] = set()
        for k in keys:
            try:
                out.add(int(k.rsplit(":", 1)[-1]))
            except ValueError:
                continue
        return out
    except Exception:
        return None
