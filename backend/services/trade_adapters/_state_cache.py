"""Per-account, per-symbol leverage/margin-mode state cache.

Skips redundant set_leverage() + set_margin_type() calls when the requested
state matches what was last applied for this (exchange, account, symbol).
Typical win: a user opens 3-5 arb legs on the same symbol in quick succession
— only the first tick actually hits the exchange's set-leverage endpoints,
subsequent ticks short-circuit.

State is scoped per-account via a hash of the API key. It's not persisted —
a fetcher restart starts with a clean cache, meaning every user's first
order will legitimately call set_leverage (safe default).

TTL defaults to 10 min so transient settings drift (e.g. user changed it on
the exchange UI mid-session) gets rechecked within that window.
"""
from __future__ import annotations

import hashlib
import time
from typing import Optional

_DEFAULT_TTL_S = 600.0  # 10 min

# (exchange, acct_hash, symbol) → (leverage, margin_mode, applied_at)
_applied: dict[tuple[str, str, str], tuple[int, str, float]] = {}


def _acct_key(creds: dict) -> str:
    """Hash enough of the credentials to identify the account without
    storing them in-memory in plain form. api_key is stable per account and
    sufficient for cache keying."""
    api_key = (creds or {}).get("api_key") or (creds or {}).get("wallet_address") or ""
    return hashlib.sha256(api_key.encode("utf-8", "ignore")).hexdigest()[:12]


def matches(exchange: str, creds: dict, symbol: str,
            leverage: int, margin_mode: str, ttl_s: float = _DEFAULT_TTL_S) -> bool:
    """Return True if the same (leverage, margin_mode) was already applied
    for this (exchange, account, symbol) within ttl_s. Caller should skip
    the API calls in that case."""
    key = (exchange, _acct_key(creds), symbol.upper())
    entry = _applied.get(key)
    if not entry:
        return False
    lev, mm, ts = entry
    if lev != int(leverage) or mm != margin_mode:
        return False
    return (time.time() - ts) < ttl_s


def record(exchange: str, creds: dict, symbol: str,
           leverage: int, margin_mode: str) -> None:
    """Remember that (leverage, margin_mode) is now applied for
    (exchange, account, symbol). Called on successful set_leverage."""
    key = (exchange, _acct_key(creds), symbol.upper())
    _applied[key] = (int(leverage), margin_mode, time.time())


def invalidate(exchange: str, creds: dict, symbol: str) -> None:
    """Force the next set_leverage for this key to hit the exchange.
    Call when an order fails with a leverage/margin-mode error — the cache
    may be stale relative to actual account state."""
    key = (exchange, _acct_key(creds), symbol.upper())
    _applied.pop(key, None)
