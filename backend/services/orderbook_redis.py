"""Redis-backed orderbook cache for the /orderbook HTTP endpoint.

Writer: the master merger (subprocess) calls `write_books(merged)` after
every books.json dump. Every `{exchange}:{symbol}` entry lands as one
Redis key `ob:{ex}:{sym}` with TTL 10 s.

Reader: `get_cached_orderbook` in `orderbook_cache.py` checks Redis
before falling back to the shared file cache. O(1) lookup replaces the
6.5 MB books.json re-parse — 237-580 ms → 1-3 ms per request.

Redis unreachable is a no-op — both writer and reader fall through to
the file cache, which is the existing path.
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("avalant.orderbook_redis")

_REDIS_URL = os.environ.get("REDIS_URL") or ""
_TTL_S = 10
_client = None
_last_failure_ts: float = 0.0
_BACKOFF_S = 10.0

try:
    import orjson as _orjson

    def _dumps(o) -> bytes:
        return _orjson.dumps(o)

    def _loads(b):
        return _orjson.loads(b)
except ImportError:
    import json as _json

    def _dumps(o) -> bytes:
        return _json.dumps(o, separators=(",", ":")).encode()

    def _loads(b):
        if isinstance(b, (bytes, bytearray)):
            b = b.decode()
        return _json.loads(b)


def _get_client():
    global _client, _last_failure_ts
    if not _REDIS_URL:
        return None
    if _client is not None:
        return _client
    if time.time() - _last_failure_ts < _BACKOFF_S:
        return None
    try:
        import redis
        _client = redis.from_url(
            _REDIS_URL,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
            health_check_interval=30,
        )
        _client.ping()
        logger.info("orderbook_redis: connected to %s", _REDIS_URL)
        return _client
    except Exception as exc:
        _client = None
        _last_failure_ts = time.time()
        logger.warning("orderbook_redis: connect failed (%s) — falling back to file cache", exc)
        return None


def write_books(merged: dict) -> int:
    """Pipeline-write every merged entry. Returns number of keys written."""
    c = _get_client()
    if c is None or not merged:
        return 0
    try:
        pipe = c.pipeline(transaction=False)
        n = 0
        for key, entry in merged.items():
            if not isinstance(entry, dict):
                continue
            pipe.setex(f"ob:{key}", _TTL_S, _dumps(entry))
            n += 1
            # Flush every 500 ops to keep pipeline memory bounded on large
            # merges (~2000 books).
            if n % 500 == 0:
                pipe.execute()
                pipe = c.pipeline(transaction=False)
        pipe.execute()
        return n
    except Exception as exc:
        logger.warning("orderbook_redis write failed: %s", exc)
        return 0


def read_book(exchange: str, symbol: str) -> dict | None:
    """Return {ts, data} for `ob:{exchange}:{symbol}` or None if unavailable."""
    c = _get_client()
    if c is None:
        return None
    try:
        raw = c.get(f"ob:{exchange}:{symbol}")
        if not raw:
            return None
        return _loads(raw)
    except Exception as exc:
        logger.debug("orderbook_redis read failed: %s", exc)
        return None
