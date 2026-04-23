"""/api/health/fetcher — observability for the multiprocess orderbook fetcher."""
from __future__ import annotations

import json
import os
import tempfile
import time


def test_health_fetcher_single_mode(client, monkeypatch):
    """No fetcher_workers.json on disk → endpoint reports single-process mode."""
    # Point the file path at an empty temp dir so `read_workers_health` misses.
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(
            "backend.services.orderbook_ws_master._HEALTH_FILE",
            os.path.join(td, "fetcher_workers.json"),
        )
        r = client.get("/api/health/fetcher")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "single"


def test_health_fetcher_multiprocess_mode(client, monkeypatch):
    """Valid fetcher_workers.json → endpoint reflects workers + books ages."""
    with tempfile.TemporaryDirectory() as td:
        # Stub fetcher_workers.json + books.<ex>.json so the endpoint can
        # surface both without touching real cache state.
        health_file = os.path.join(td, "fetcher_workers.json")
        for ex in ("binance", "bybit"):
            with open(os.path.join(td, f"books.{ex}.json"), "w") as f:
                f.write("{}")

        now = time.time()
        payload = {
            "ts": now,
            "workers": [
                {
                    "exchange": "binance", "pid": 111, "alive": True,
                    "started_at": now - 42, "uptime_s": 42,
                    "restarts_1m": 0, "exit_count": 0, "last_exit_rc": None,
                },
                {
                    "exchange": "bybit", "pid": 112, "alive": True,
                    "started_at": now - 9, "uptime_s": 9,
                    "restarts_1m": 1, "exit_count": 1, "last_exit_rc": 0,
                },
            ],
        }
        with open(health_file, "w") as f:
            json.dump(payload, f)

        monkeypatch.setattr(
            "backend.services.orderbook_ws_master._HEALTH_FILE", health_file,
        )
        # The endpoint also stats books.<ex>.json from /tmp/avalant_cache/ —
        # monkey-patch that join-base for the test to use our temp dir.
        import backend.api.v1.health as _health_mod
        orig_join = os.path.join

        def _fake_join(*parts):
            if parts[:1] == ("/tmp/avalant_cache",):
                return orig_join(td, *parts[1:])
            return orig_join(*parts)

        monkeypatch.setattr(_health_mod.os.path, "join", _fake_join)

        r = client.get("/api/health/fetcher")

    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "multiprocess"
    assert len(body["workers"]) == 2
    assert {w["exchange"] for w in body["workers"]} == {"binance", "bybit"}
    for w in body["workers"]:
        assert "output_file" in w
        assert "output_age_s" in w
        assert "output_size" in w
