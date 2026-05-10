"""6-digit email confirmation codes for sensitive ops.

Used as an alternative to the password gate on /me/2fa/setup, /disable,
/recovery-codes/regenerate for users who registered via OAuth and don't
have a local password.

Storage:
    Redis key  : `email_confirm:<user_id>` → bcrypt(code)
    TTL        : 10 minutes
    Single-use : verify() deletes the key on success

Code shape : 6 digits, zero-padded. Easy to type on mobile.
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Optional

from passlib.hash import bcrypt

logger = logging.getLogger("avalant.email_confirm")

TTL_SECONDS = 600  # 10 min


def _redis():
    """Lazy-load redis client. Returns None if REDIS_URL unset (callers
    fall back to in-memory dict for dev)."""
    try:
        import redis  # type: ignore
    except ImportError:
        return None
    url = os.environ.get("REDIS_URL") or ""
    if not url:
        return None
    try:
        return redis.from_url(url, decode_responses=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis connect failed: %s", exc)
        return None


# In-memory fallback for environments without Redis (local dev).
# Not safe across multiple workers; production must use Redis.
_mem: dict[str, str] = {}


def _key(user_id: int) -> str:
    return f"email_confirm:{user_id}"


def issue(user_id: int) -> str:
    """Generate a fresh 6-digit code, store the bcrypt-hash with TTL,
    return the plaintext for the caller to email."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    h = bcrypt.hash(code)
    r = _redis()
    k = _key(user_id)
    if r is not None:
        try:
            r.setex(k, TTL_SECONDS, h)
        except Exception:
            _mem[k] = h
    else:
        _mem[k] = h
    return code


def verify(user_id: int, code: str) -> bool:
    """Verify a submitted 6-digit code against the stored hash. Single-use:
    the stored hash is deleted on success."""
    if not code:
        return False
    code = code.strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return False
    r = _redis()
    k = _key(user_id)
    stored: Optional[str] = None
    if r is not None:
        try:
            stored = r.get(k)
        except Exception:
            stored = None
    if stored is None:
        stored = _mem.get(k)
    if not stored:
        return False
    try:
        if bcrypt.verify(code, stored):
            # Single-use — invalidate so a leaked code can't be replayed.
            if r is not None:
                try: r.delete(k)
                except Exception: pass
            _mem.pop(k, None)
            return True
    except Exception:
        return False
    return False
