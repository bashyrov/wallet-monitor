"""Per-stream live snapshot — positions + balance, kept fresh by WS
events. Mirrored to Redis so the web replica reads from the same
state the fetcher's supervisor writes.

Layout in Redis:

  HSET avalant:userstream:positions:<user_id>:<wallet_id>
       <symbol> → JSON blob
  EX 600s on the hash (gets refreshed on every position event)

  SET avalant:userstream:status:<user_id>:<wallet_id>
       LIVE | DEGRADED | DEAD
  EX 60s — heartbeat. Stale = no live stream, fall back to REST.

Read path (list_user_positions):
  1. Check status key. If LIVE within last 60s → use snapshot.
  2. Else → REST as before.

The in-process dict mirrors Redis so the fetcher process serves its
own reads without round-tripping. Redis is only for cross-process
visibility (web ← fetcher).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger("avalant.userstream.snapshot")

_TTL_POSITIONS_S = 600   # hash expiry — refreshed on every event
_TTL_STATUS_S = 60       # heartbeat — TTL > write cadence (heartbeat every 30s)

# In-process mirror — fetcher serves its own reads from here
_local_positions: dict[tuple[int, int], dict[str, dict]] = {}
_local_balance: dict[tuple[int, int], float | None] = {}
_local_status: dict[tuple[int, int], tuple[str, float]] = {}

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
        logger.debug("userstream snapshot redis connect failed: %s", exc)
        return None


def _hkey_positions(user_id: int, wallet_id: int) -> str:
    return f"avalant:userstream:positions:{user_id}:{wallet_id}"


def _key_status(user_id: int, wallet_id: int) -> str:
    return f"avalant:userstream:status:{user_id}:{wallet_id}"


def _key_balance(user_id: int, wallet_id: int) -> str:
    return f"avalant:userstream:balance:{user_id}:{wallet_id}"


def update_position(user_id: int, wallet_id: int, exchange: str,
                    symbol: str, payload: dict) -> None:
    """Set the snapshot for one symbol. payload should match the shape
    list_user_positions expects (so callers can return it directly).
    `qty=0` deletes the symbol from the snapshot."""
    key = (user_id, wallet_id)
    bucket = _local_positions.setdefault(key, {})
    qty = float(payload.get("quantity") or 0)
    if qty == 0:
        bucket.pop(symbol, None)
    else:
        # Always include exchange + wallet_id so caller can serialize directly.
        payload = {**payload, "exchange": exchange, "wallet_id": wallet_id}
        bucket[symbol] = payload

    c = _redis()
    if c is not None:
        try:
            hk = _hkey_positions(user_id, wallet_id)
            if qty == 0:
                c.hdel(hk, symbol)
            else:
                c.hset(hk, symbol, json.dumps(payload, default=str))
            c.expire(hk, _TTL_POSITIONS_S)
        except Exception as exc:
            logger.debug("userstream snapshot redis write failed: %s", exc)


def update_balance(user_id: int, wallet_id: int, balance_usdt: float | None) -> None:
    key = (user_id, wallet_id)
    _local_balance[key] = balance_usdt
    c = _redis()
    if c is not None:
        try:
            c.setex(_key_balance(user_id, wallet_id), _TTL_POSITIONS_S,
                    json.dumps(balance_usdt))
        except Exception as exc:
            logger.debug("userstream snapshot balance write failed: %s", exc)


def set_status(user_id: int, wallet_id: int, status: str) -> None:
    """Heartbeat. Status TTL is 60s — if we stop ping'ing, readers fall
    back to REST automatically."""
    key = (user_id, wallet_id)
    _local_status[key] = (status, time.time())
    c = _redis()
    if c is not None:
        try:
            c.setex(_key_status(user_id, wallet_id), _TTL_STATUS_S, status)
        except Exception as exc:
            logger.debug("userstream snapshot status write failed: %s", exc)


def get_status(user_id: int, wallet_id: int) -> str | None:
    """Returns LIVE | DEGRADED | DEAD or None if no fresh status."""
    # Local first
    s = _local_status.get((user_id, wallet_id))
    if s and (time.time() - s[1]) < _TTL_STATUS_S:
        return s[0]
    # Redis fallback (cross-process)
    c = _redis()
    if c is None:
        return None
    try:
        val = c.get(_key_status(user_id, wallet_id))
        return val
    except Exception:
        return None


def get_positions(user_id: int, wallet_id: int) -> list[dict] | None:
    """Returns the cached list of positions for this wallet, or None if
    no fresh snapshot. Returned shape matches list_user_positions output
    so callers can serve it as-is."""
    # Local first
    bucket = _local_positions.get((user_id, wallet_id))
    if bucket is not None:
        return list(bucket.values())
    # Redis fallback
    c = _redis()
    if c is None:
        return None
    try:
        data = c.hgetall(_hkey_positions(user_id, wallet_id))
        if not data:
            return []
        out = []
        for raw in data.values():
            try:
                out.append(json.loads(raw))
            except Exception:
                continue
        return out
    except Exception:
        return None


def clear_wallet(user_id: int, wallet_id: int) -> None:
    """Drop everything we have for one wallet — called when stream stops."""
    key = (user_id, wallet_id)
    _local_positions.pop(key, None)
    _local_balance.pop(key, None)
    _local_status.pop(key, None)
    c = _redis()
    if c is not None:
        try:
            c.delete(_hkey_positions(user_id, wallet_id),
                     _key_status(user_id, wallet_id),
                     _key_balance(user_id, wallet_id))
        except Exception:
            pass
