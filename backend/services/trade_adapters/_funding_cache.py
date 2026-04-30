"""Per-symbol funding-PnL cache shared across all trade adapters.

Most adapters fetch funding PnL by looping over each open position,
calling the exchange's per-symbol income/bills endpoint. Each call is
1-30 weight on the venue's rate-limit budget — and `list_positions` is
called every 10 seconds while the user has /arb open. With N positions
that's N×6 calls per minute, which trivially trips per-IP bans for
Binance, MEXC, etc.

This cache memoises every `_funding_pnl(creds, symbol, since_ms)` call
for 30 seconds, keyed by (api_key, symbol). Funding changes only at the
top of the funding hour anyway, so 30s freshness is more than enough.

For adapters whose API supports a no-symbol bulk fetch (Binance fork
endpoints, OKX bills, Gate account_book, etc.), the adapter itself
provides a `_funding_pnl_bulk` that gets the whole table in one call —
strictly better than this per-symbol cache. This module is the fallback
for everyone else.
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable

_CACHE: dict[tuple[str, str], tuple[float, float | None]] = {}
_TTL_S = 30.0


async def cached_funding(
    api_key: str,
    symbol: str,
    fetch: Callable[[], Awaitable[float | None]],
) -> float | None:
    key = ((api_key or "").strip(), symbol)
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < _TTL_S:
        return hit[1]
    val = await fetch()
    _CACHE[key] = (time.time(), val)
    return val
