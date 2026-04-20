"""Avalant fetcher — standalone process that owns the data plane.

Runs outside the uvicorn process so its event loop is never starved by
user traffic. Writes shared state to /tmp/avalant_cache/*.json which
the web workers read.

Started via `python -m fetcher` (docker compose service).
"""
