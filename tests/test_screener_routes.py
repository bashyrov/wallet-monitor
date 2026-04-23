"""Screener route smoke tests — every canonical + legacy HTTP/WS endpoint
must register and answer non-500 when the data-plane is cold.

Backstory: the Long/Short rename kept deprecated aliases (/arbitrage,
/spot-arbitrage, /dex-arbitrage, /ws/arb) live. Without a test, a cleanup
sweep could silently nuke the alias and break every existing bookmark.
Also catches IndentationError in the handler itself — the router import
exercises the module-level code path.
"""
from __future__ import annotations

import pytest


CANONICAL_ROUTES = [
    "/api/screener/long-short",
    "/api/screener/spot-short",
    "/api/screener/dex-short",
    "/api/screener/all-arbitrage",
    "/api/screener/funding",
    "/api/screener/exchange-health",
]

LEGACY_ROUTES = [
    # These aliases MUST stay until frontend is fully migrated. Removing one
    # is a breaking change — this test fails then so it's an explicit decision.
    "/api/screener/arbitrage",
    "/api/screener/spot-arbitrage",
    "/api/screener/dex-arbitrage",
]


@pytest.mark.parametrize("path", CANONICAL_ROUTES + LEGACY_ROUTES)
def test_screener_route_registered(client, path):
    """Every route must exist. 200 or 503 is OK (503 = cold start / upstream
    down); 404 means a route was accidentally deleted."""
    r = client.get(path)
    assert r.status_code != 404, f"{path} not registered — did a cleanup sweep drop it?"
    assert r.status_code < 500 or r.status_code in (502, 503), \
        f"{path} returned {r.status_code}: {r.text[:200]}"


def test_ws_book_endpoint_registered():
    """New /ws/book is the foundation of the 500-user scale plan — if it
    disappears, /arb silently falls back to 150ms HTTP poll and breaks
    everything."""
    from app import app
    ws_paths = {r.path for r in app.routes if getattr(r, "path", "").startswith("/api/screener/ws/")}
    required = {"/api/screener/ws/funding", "/api/screener/ws/long-short",
                "/api/screener/ws/arb", "/api/screener/ws/book"}
    missing = required - ws_paths
    assert not missing, f"missing WS endpoints: {missing}"


def test_availability_endpoint_registered(client):
    r = client.get("/api/screener/availability")
    assert r.status_code != 404


def test_orderbook_endpoint_registered(client):
    r = client.get("/api/screener/orderbook?symbol=BTC&exchange=binance&limit=10")
    # May 503 under cold-start; never 404.
    assert r.status_code != 404
