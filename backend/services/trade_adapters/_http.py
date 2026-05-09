"""Per-host persistent httpx.AsyncClient pool for trade adapters.

Every adapter previously created a brand-new `httpx.AsyncClient(timeout=N)`
per signed call — that pays a fresh TCP+TLS handshake on every order,
balance, or position request. SG→SG handshake is ~30-50ms; SG→US is
~200-300ms. With a persistent client per host, the handshake is paid
once per process per host, then keepalive reuses the same TCP/TLS
connection for every subsequent call.

For order placement specifically, that's a 100-300ms saving per order
on top of the actual venue processing time (~50ms). For the user this
is the difference between "instant" and "noticeable lag".

Use:
    client = http_client("https://api.bybit.com")
    r = await client.get("/v5/account/wallet-balance", headers=...)

Singleton per base URL. Closed automatically when the process exits.
"""
from __future__ import annotations

import threading
from typing import Optional

import httpx

_clients: dict[str, httpx.AsyncClient] = {}
_lock = threading.Lock()


def http_client(base_url: str, *, timeout: float = 10.0) -> httpx.AsyncClient:
    """Return a persistent AsyncClient pinned to `base_url`. Idempotent."""
    key = base_url.rstrip("/")
    c = _clients.get(key)
    if c is not None and not c.is_closed:
        return c
    with _lock:
        c = _clients.get(key)
        if c is not None and not c.is_closed:
            return c
        # max_keepalive_connections=20: typical workload bursts to 5-10
        # parallel adapter calls, 20 leaves headroom; 50 cap protects
        # us from a burst going wild. keepalive_expiry=300s aligns with
        # most CDNs/exchanges idle-keepalive grace.
        c = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=300,
            ),
        )
        _clients[key] = c
    return c


async def aclose_all() -> None:
    """Close every client. Called only on process shutdown."""
    for c in list(_clients.values()):
        try:
            await c.aclose()
        except Exception:
            pass
    _clients.clear()
