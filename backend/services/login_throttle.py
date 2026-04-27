"""Per-account login throttle. Replaces the previous "5 strikes → permanent
lockout" rule, which doubled as a DoS vector: anyone who knew a target's
username could lock them out by hammering the wrong password.

Design — exponential cooldown per login string:
  · first 3 failures        → no cooldown (typo grace), 500 ms response delay
  · failure 4               → 30 s cooldown before the next attempt is accepted
  · failure 5               → 60 s
  · failure 6               → 120 s
  · …doubling each time, capped at 1 h (failure 11+)
  · 30 min of no failures, OR one successful login, resets the counter

Constant 500 ms response delay on every failed attempt makes automated brute-
force expensive even before the cooldown trips: ~7200 attempts/h ceiling per
account before throttle kicks in, vs. tens-of-thousands previously.

Storage: Redis when REDIS_URL is set (so both web replicas see the same
counter); falls back to a process-local dict otherwise.
"""

import asyncio
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger("avalant.login_throttle")

_FAIL_TTL_S = 1800        # sliding window — failures older than this don't count
_COOLDOWN_BASE_S = 30     # cooldown after the 4th failure
_COOLDOWN_CAP_S = 3600    # absolute cap (1 hour)
_FREE_FAILS = 3           # first N failures: no cooldown, just response delay
_RESPONSE_DELAY_S = 0.5   # constant per-failure delay (timing-side-channel)

_redis_client = None

# In-memory fallback when Redis is unavailable. Tuple shape per key:
#   (fail_count, fail_ttl_expires_at, block_until_ts)
_fallback: dict[str, tuple[int, float, float]] = {}
_fallback_lock = threading.Lock()


def _redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    url = os.environ.get("REDIS_URL") or ""
    if not url:
        return None
    try:
        import redis
        _redis_client = redis.from_url(url, socket_connect_timeout=1.0, socket_timeout=1.5)
        _redis_client.ping()
        return _redis_client
    except Exception as exc:
        logger.warning("login_throttle: redis unavailable (%s) — falling back to in-memory", exc)
        _redis_client = None
        return None


def _norm(subject: str) -> str:
    return (subject or "").strip().lower()


def _fail_key(subject: str, scope: str = "login") -> str:
    return f"{scope}_fail:{_norm(subject)}"


def _block_key(subject: str, scope: str = "login") -> str:
    return f"{scope}_block:{_norm(subject)}"


def _cooldown_for(failures: int) -> int:
    """Map a running failure count to a cooldown in seconds."""
    if failures <= _FREE_FAILS:
        return 0
    return min(_COOLDOWN_BASE_S * (2 ** (failures - _FREE_FAILS - 1)), _COOLDOWN_CAP_S)


def check(login: str) -> Optional[int]:
    """Return seconds-until-allowed if the account is currently in cooldown,
    else None. Caller should bail with 429 + Retry-After when this is set."""
    if not login:
        return None
    rc = _redis()
    if rc is not None:
        try:
            ttl = rc.ttl(_block_key(login))
            return int(ttl) if ttl and ttl > 0 else None
        except Exception:
            pass
    # In-memory fallback
    now = time.time()
    with _fallback_lock:
        rec = _fallback.get(_norm(login))
        if not rec:
            return None
        _, _, block_until = rec
        if block_until > now:
            return int(block_until - now)
    return None


def register_failure(login: str) -> int:
    """Record a failed login. Returns the cooldown (seconds) the caller
    should report — 0 means no cooldown yet (still in the typo-grace zone)."""
    if not login:
        return 0
    rc = _redis()
    if rc is not None:
        try:
            n = rc.incr(_fail_key(login))
            rc.expire(_fail_key(login), _FAIL_TTL_S)
            cooldown = _cooldown_for(int(n))
            if cooldown > 0:
                rc.set(_block_key(login), str(int(n)), ex=cooldown)
            return cooldown
        except Exception as exc:
            logger.warning("login_throttle: redis register_failure failed (%s)", exc)
    # In-memory fallback
    now = time.time()
    with _fallback_lock:
        rec = _fallback.get(_norm(login))
        if rec and rec[1] > now:
            n = rec[0] + 1
        else:
            n = 1
        cooldown = _cooldown_for(n)
        _fallback[_norm(login)] = (n, now + _FAIL_TTL_S, now + cooldown if cooldown else 0.0)
    return cooldown


def clear(login: str) -> None:
    """Reset the counter — call on successful login."""
    if not login:
        return
    rc = _redis()
    if rc is not None:
        try:
            rc.delete(_fail_key(login), _block_key(login))
            return
        except Exception:
            pass
    with _fallback_lock:
        _fallback.pop(_norm(login), None)


async def response_delay() -> None:
    """Constant pause on every failed-login response. Independent of the
    cooldown — even an attacker still in the typo-grace zone pays this."""
    await asyncio.sleep(_RESPONSE_DELAY_S)
