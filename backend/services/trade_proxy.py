"""HTTP proxy from Python web role into the Go trade engine.

Per-call dispatch: the Python web role's `trade_service.place_open_order`
checks the proxy here first. If the venue is on `GO_TRADE_VENUES` AND the
go-fetcher is reachable, we POST `/internal/trade/open` and return the
Go-side response directly. Any failure (network blip, 5xx, unsupported
venue, missing auth header) falls back to the local Python adapter so a
proxy outage NEVER leaves a user unable to trade.

Wire-up: set the env vars on every web replica + on go-fetcher

    AVALANT_TRADE_PROXY_URL=http://go-fetcher:8090   # internal name
    AVALANT_INTERNAL_SECRET=<long-random>            # same on both sides
    GO_TRADE_VENUES=binance                          # comma-separated cutover list

The cutover list is intentionally narrow — flip one venue at a time and
watch error rates. Empty = proxy disabled, all calls stay on Python.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("avalant.trade.proxy")

_DEFAULT_URL = "http://go-fetcher:8090"
# connect=2s was too tight when Portfolio refresh concurrently pinged
# the proxy for every screener-eligible wallet — DNS + 3-way handshake
# under load missed the window. 5s is generous, and read/write stay
# small so a hung Go side still surfaces fast.
_TIMEOUT = httpx.Timeout(connect=5.0, read=12.0, write=4.0, pool=4.0)


def _enabled_venues() -> set[str]:
    raw = (os.environ.get("GO_TRADE_VENUES") or "").strip()
    if not raw:
        return set()
    return {v.strip().lower() for v in raw.split(",") if v.strip()}


def _proxy_url() -> str:
    return (os.environ.get("AVALANT_TRADE_PROXY_URL") or _DEFAULT_URL).rstrip("/")


def _secret() -> str:
    return os.environ.get("AVALANT_INTERNAL_SECRET", "").strip()


def is_enabled(exchange: str) -> bool:
    """Should this exchange go through the Go engine?"""
    if not _secret():
        return False
    return exchange.lower() in _enabled_venues()


# ── Error envelope ──────────────────────────────────────────────────────────

class GoTradeError(Exception):
    """Raised when go-fetcher returns a non-2xx response. Carries `kind`
    so trade_service can map it to the same TradeError(kind=...) shape
    the Python adapters produce."""
    def __init__(self, kind: str, message: str, code: str | None = None):
        super().__init__(message)
        self.kind = kind or "internal"
        self.message = message
        self.code = code

    def __repr__(self):
        return f"GoTradeError(kind={self.kind!r}, code={self.code!r}, msg={self.message!r})"


async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    url = _proxy_url() + path
    headers = {"X-Internal-Auth": _secret(), "Content-Type": "application/json"}
    # Brief retry on connect-error — Docker DNS or the SO_BACKLOG queue
    # occasionally drops a single connect attempt under spikes (Portfolio
    # refresh fans out N parallel calls). One retry with backoff fixes
    # virtually all of them; if both fail we surface as transient.
    last_err = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.post(url, json=body, headers=headers)
            break
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_err = e
            if attempt == 0:
                import asyncio as _a
                await _a.sleep(0.15)
                continue
            raise GoTradeError("transient", f"proxy network error: {e!r}") from e
        except httpx.RequestError as e:
            raise GoTradeError("transient", f"proxy network error: {e!r}") from e
    else:
        raise GoTradeError("transient", f"proxy network error: {last_err!r}")
    if r.status_code == 204:
        return {}
    try:
        payload = r.json()
    except Exception:
        payload = {"error": r.text or "<no body>"}
    if r.status_code >= 400:
        raise GoTradeError(
            kind=payload.get("kind", "internal"),
            message=payload.get("error") or "proxy returned error",
            code=payload.get("code"),
        )
    return payload


# ── Public surface — mirror of trade_adapters/_base.py shape ────────────────

async def place_order(
    exchange: str, creds: dict, symbol: str, side: str, quantity: float,
    leverage: int = 1, margin_mode: str = "isolated",
    market_type: str = "futures",
) -> dict:
    """Forward a place-order to the Go engine. Output shape matches what
    trade_adapters/<ex>.py.place_order returns (order_id + avg_price)."""
    request = {
        "symbol": symbol.upper(),
        "side": side,
        "quantity": float(quantity),
        "leverage": int(leverage),
        "margin_mode": margin_mode,
    }
    if market_type and market_type != "futures":
        # Only emit when non-default — keeps the wire shape backward
        # compatible for existing futures-only flows.
        request["market_type"] = market_type
    body = {
        "exchange": exchange.lower(),
        "creds": _strip_creds(exchange, creds),
        "request": request,
    }
    out = await _post("/internal/trade/open", body)
    return {
        "order_id": out.get("order_id"),
        "avg_price": float(out.get("avg_price") or 0),
        "status": out.get("status"),
        "client_order_id": out.get("client_order_id"),
    }


async def close_position(exchange: str, creds: dict, symbol: str, side: str,
                          market_type: str = "futures") -> dict:
    request = {"symbol": symbol.upper(), "side": side}
    if market_type and market_type != "futures":
        request["market_type"] = market_type
    body = {
        "exchange": exchange.lower(),
        "creds": _strip_creds(exchange, creds),
        "request": request,
    }
    out = await _post("/internal/trade/close", body)
    return {
        "order_id": out.get("order_id"),
        "closed_qty": float(out.get("quantity") or 0),
        "avg_price": float(out.get("avg_price") or 0),
        "status": out.get("status"),
    }


async def set_leverage(
    exchange: str, creds: dict, symbol: str, leverage: int, margin_mode: str,
) -> None:
    body = {
        "exchange": exchange.lower(),
        "creds": _strip_creds(exchange, creds),
        "request": {
            "symbol": symbol.upper(),
            "leverage": int(leverage),
            "margin_mode": margin_mode,
        },
    }
    await _post("/internal/trade/leverage", body)


async def list_positions(exchange: str, creds: dict, symbol: str | None = None) -> list[dict]:
    body = {"exchange": exchange.lower(), "creds": _strip_creds(exchange, creds)}
    if symbol:
        body["symbol"] = symbol.upper()
    out = await _post("/internal/trade/positions", body)
    if not isinstance(out, list):
        return []
    return out


async def fetch_balance(exchange: str, creds: dict) -> dict:
    body = {"exchange": exchange.lower(), "creds": _strip_creds(exchange, creds)}
    out = await _post("/internal/trade/balance", body)
    return {
        "usdt": float(out.get("available_usd") or 0),
        "total": float(out.get("total_usd") or 0),
        "margin": float(out.get("margin_usd") or 0),
    }


# ── Per-venue cred translation ──
# Wallet storage uses venue-friendly names (address, l2_private_key, etc).
# Go's Creds struct uses canonical (api_key, api_secret, passphrase). We
# translate at the proxy boundary so storage stays human-readable and the
# wire stays simple.
def _normalize_for_venue(exchange: str, creds: dict) -> dict:
    ex = (exchange or "").lower()
    if ex == "paradex":
        # Paradex Wallet creds: address + private_key + (api_passphrase = subkey pubkey)
        # Go expects:           api_key + api_secret + passphrase
        out = dict(creds)
        if not out.get("api_key") and out.get("address"):
            out["api_key"] = out["address"]
        if not out.get("api_secret") and out.get("private_key"):
            out["api_secret"] = out["private_key"]
        # api_passphrase already lines up with `passphrase` via _strip_creds.
        return out
    if ex == "hyperliquid":
        # HL wallet stores address + api_secret (= EVM private key). Go
        # already accepts those names, but mirror api_secret → private_key
        # so older Go branches that read either keep working.
        out = dict(creds)
        if not out.get("api_key") and out.get("address"):
            out["api_key"] = out["address"]
        return out
    return creds


def _strip_creds(exchange: str, creds: dict) -> dict:
    """Pick only the fields Go's Creds struct knows about. Reduces the
    risk of leaking unrelated metadata across the wire."""
    if not isinstance(creds, dict):
        return {}
    creds = _normalize_for_venue(exchange, creds)
    out = {}
    # Note: api_passphrase is the wallet-side name; Go's struct field
    # is `passphrase`. Map both into the same slot.
    if creds.get("api_passphrase") and not creds.get("passphrase"):
        creds = dict(creds)
        creds["passphrase"] = creds["api_passphrase"]
    for k in ("api_key", "api_secret", "passphrase", "wallet", "private_key", "uid"):
        v = creds.get(k)
        if v:
            out[k] = v
    extra = {}
    for k, v in creds.items():
        if k in out or k.startswith("_") or v is None:
            continue
        if k in ("api_key", "api_secret", "passphrase", "wallet", "private_key", "uid",
                 "api_passphrase", "address"):
            continue
        extra[k] = str(v)
    if extra:
        out["extra"] = extra
    return out
