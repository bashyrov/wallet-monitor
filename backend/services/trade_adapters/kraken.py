"""Kraken Futures linear-perp trade adapter.

Authentication (different from Kraken Spot):
  hash      = SHA256(postData + nonce + endpointPath)
  signature = HMAC-SHA512(base64_decode(secret), hash)
  authent   = base64_encode(signature)
  headers   = APIKey, Nonce, Authent

Symbol convention: PF_<TOKEN>USD (linear, USD-collateralised). XBT alias
applies for BTC. Order quantity is in CONTRACTS where 1 contract = 1
unit of the base token (no contract-multiplier complication on PF_).

Reference: https://docs.kraken.com/api/docs/futures-api/trading
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("avalant.trade.kraken")

BASE = "https://futures.kraken.com"
ROOT = "/derivatives/api/v3"


def _norm_symbol(sym: str) -> str:
    sym = (sym or "").upper()
    if sym == "BTC":
        sym = "XBT"
    return f"PF_{sym}USD"


def _denorm_symbol(pf_sym: str) -> str:
    """PF_XBTUSD → BTC, PF_ETHUSD → ETH."""
    s = pf_sym
    if s.startswith("PF_"):
        s = s[3:]
    if s.endswith("USD"):
        s = s[:-3]
    if s == "XBT":
        s = "BTC"
    return s


# ── Instrument cache (size precision per market) ─────────────────────────────
_INSTR_CACHE: dict[str, dict] = {}
_INSTR_TS: float = 0.0
_INSTR_TTL = 1800.0
_INSTR_LOCK = asyncio.Lock()


async def _instruments() -> dict[str, dict]:
    global _INSTR_TS
    if _INSTR_CACHE and (time.monotonic() - _INSTR_TS) < _INSTR_TTL:
        return _INSTR_CACHE
    async with _INSTR_LOCK:
        if _INSTR_CACHE and (time.monotonic() - _INSTR_TS) < _INSTR_TTL:
            return _INSTR_CACHE
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(f"{BASE}{ROOT}/instruments")
                items = (r.json() or {}).get("instruments") or []
        except Exception:
            return _INSTR_CACHE
        out = {}
        for it in items:
            sym = it.get("symbol") or ""
            if not sym.startswith("PF_"):
                continue
            out[sym] = {
                "tick_size": float(it.get("tickSize") or 0),
                "contract_size": float(it.get("contractSize") or 1),
                "max_position_size": float(it.get("maxPositionSize") or 0),
                "category": it.get("category") or "",
            }
        if out:
            _INSTR_CACHE.clear()
            _INSTR_CACHE.update(out)
            _INSTR_TS = time.monotonic()
        return _INSTR_CACHE


def _sign(api_secret: str, post_data: str, nonce: str, path: str) -> str:
    """Kraken futures signature: SHA256(postData + nonce + path) then
    HMAC-SHA512 with base64-decoded secret, output base64."""
    msg = (post_data + nonce + path).encode()
    hashed = hashlib.sha256(msg).digest()
    secret_decoded = base64.b64decode(api_secret)
    sig = hmac.new(secret_decoded, hashed, hashlib.sha512).digest()
    return base64.b64encode(sig).decode()


async def _signed_request(creds: dict, method: str, path: str,
                           params: dict | None = None) -> Any:
    api_key = (creds.get("api_key") or "").strip()
    api_secret = (creds.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("Kraken Futures requires api_key and api_secret")
    post_data = urlencode(params or {})
    # Path used in the signature is RELATIVE — Kraken docs use endpoint path
    # without the /derivatives/api/v3 prefix (e.g. "/accounts").
    # The full URL still uses ROOT.
    sig_path = path
    nonce = str(int(time.time() * 1000))
    authent = _sign(api_secret, post_data, nonce, sig_path)
    headers = {
        "APIKey": api_key,
        "Nonce": nonce,
        "Authent": authent,
        "Accept": "application/json",
    }
    from backend.services.trade_adapters._http import http_client
    client = http_client(BASE, timeout=10.0)
    rel = ROOT + path
    if method == "GET":
        if post_data:
            rel += "?" + post_data
        r = await client.get(rel, headers=headers)
    else:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        r = await client.post(rel, headers=headers, content=post_data)
    if r.status_code >= 400:
        raise RuntimeError(f"Kraken {r.status_code}: {r.text[:200]}")
    data = r.json()
    if isinstance(data, dict) and data.get("result") == "error":
        raise RuntimeError(f"Kraken: {data.get('error') or data}")
    return data


class KrakenAdapter:
    EXCHANGE = "kraken"
    DISPLAY = "Kraken"

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        data = await _signed_request(creds, "GET", "/accounts")
        # accounts.flex.balanceValue is the USD value of the multi-collateral
        # flex account (the standard portfolio account on Kraken Futures).
        accounts = (data or {}).get("accounts") or {}
        flex = accounts.get("flex") or {}
        usdt = float(flex.get("balanceValue") or 0)
        if usdt == 0:
            # Older accounts have a "cash" section keyed by currency — sum USD/USDT
            cash = (accounts.get("cash") or {}).get("balances") or {}
            usdt = float(cash.get("USD") or 0) + float(cash.get("USDT") or 0)
        return {"usdt": usdt}

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}

        def _friendly(e: Exception, fallback: str) -> str:
            msg = str(e)
            ml = msg.lower()
            if "authenticationerror" in ml or "401" in ml or "invalid api key" in ml:
                return "Invalid API key — check key, secret, and that it's enabled for Futures (not Spot)"
            if "signaturedidnotmatch" in ml or "signature" in ml:
                return "Signature mismatch — API secret is wrong (must be the base64 secret from the futures key page)"
            if "permission" in ml or "notauthorized" in ml or "403" in ml:
                return "API key lacks the required permission — enable General + Funding read"
            if "ipaddress" in ml or "ip" in ml and "whitelist" in ml:
                return "Server IP not whitelisted on this Kraken Futures key"
            if "ratelimited" in ml or "429" in ml:
                return "Kraken rate-limited the request — try again in a minute"
            return f"{fallback}: {msg[:140]}"

        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["balance_usdt"] = float(bal.get("usdt") or 0)
        except Exception as e:
            out["error"] = _friendly(e, "Kraken rejected the key")
            return out
        if need_trade:
            try:
                await _signed_request(creds, "GET", "/leveragepreferences")
                out["can_trade"] = True
            except Exception as e:
                out["error"] = _friendly(e, "Kraken trade permission missing")
        return out

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        # Kraken Futures generally caps at 50x for majors; venue rejects above it
        return 50

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str,
                           leverage: int, margin_mode: str) -> None:
        sym = _norm_symbol(symbol)
        try:
            await _signed_request(creds, "POST", "/leveragepreferences",
                                   {"symbol": sym, "maxLeverage": int(max(1, leverage))})
        except Exception as e:
            # Kraken may reject if leverage already at the requested value;
            # not a fatal error — trade can proceed.
            logger.info("kraken set_leverage(%s, %sx) note: %s", sym, leverage, e)

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        sym = _norm_symbol(symbol)
        is_buy = (side or "").lower() in ("buy", "long")
        params = {
            "orderType": "mkt",
            "symbol": sym,
            "side": "buy" if is_buy else "sell",
            "size": str(quantity),
        }
        data = await _signed_request(creds, "POST", "/sendorder", params)
        status = (data or {}).get("sendStatus") or {}
        order_id = status.get("order_id") or ""
        # avg_price isn't guaranteed in immediate response — pull from fills
        # if present, otherwise leave 0 and let the position sync fill it in.
        fills = status.get("fillStatus") or {}
        avg = float(fills.get("price") or 0)
        return {"order_id": str(order_id), "avg_price": avg}

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        positions = await cls.list_positions(creds, symbol=symbol)
        match = next((p for p in positions if (p.get("symbol") or "").upper() == symbol.upper()), None)
        if not match:
            return {"order_id": "", "closed_qty": 0.0, "realized_pnl_usd": 0.0}
        qty = abs(float(match.get("quantity") or 0))
        if qty <= 0:
            return {"order_id": "", "closed_qty": 0.0, "realized_pnl_usd": 0.0}
        opposite = "sell" if (match.get("side") or "").lower() == "buy" else "buy"
        sym = _norm_symbol(symbol)
        params = {
            "orderType": "mkt",
            "symbol": sym,
            "side": opposite,
            "size": str(qty),
            "reduceOnly": "true",
        }
        data = await _signed_request(creds, "POST", "/sendorder", params)
        status = (data or {}).get("sendStatus") or {}
        return {
            "order_id": str(status.get("order_id") or ""),
            "closed_qty": qty,
            "realized_pnl_usd": float(match.get("unrealized_pnl_usd") or 0),
        }

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        data = await _signed_request(creds, "GET", "/openpositions")
        positions = (data or {}).get("openPositions") or []
        out: list[dict] = []
        for p in positions:
            pf_sym = p.get("symbol") or ""
            base = _denorm_symbol(pf_sym)
            if symbol and base.upper() != symbol.upper():
                continue
            try:
                qty = float(p.get("size") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty == 0:
                continue
            side = "buy" if (p.get("side") or "").lower() == "long" else "sell"
            try:
                funding = float(p.get("unrealizedFunding") or 0)
            except (TypeError, ValueError):
                funding = 0.0
            try:
                pnl = float(p.get("pnl") or 0)
            except (TypeError, ValueError):
                pnl = 0.0
            out.append({
                "exchange": "kraken",
                "symbol": base,
                "side": side,
                "quantity": abs(qty),
                "entry_price": float(p.get("price") or 0),
                "unrealized_pnl_usd": funding + pnl,
                "funding_pnl_usd": funding,  # paid funding (negative when user pays)
                "leverage": None,
                "margin_mode": "cross",
            })
        return out

    @classmethod
    async def get_public_qty_limits(cls, symbol: str) -> dict | None:
        """Kraken Futures expresses qty in CONTRACTS where 1 contract =
        `contract_size` coins. Min order = 1 contract. Position cap from
        instrument metadata (max_position_size in coins, post-multiply)."""
        try:
            info = (await _instruments()).get(_norm_symbol(symbol))
        except Exception:
            return None
        if not info:
            return None
        cs = float(info.get("contract_size") or 1)
        max_pos = float(info.get("max_position_size") or 0) or None
        return {
            "min_qty": cs,
            "step":    cs,
            "max_qty": max_pos,
            "unit": "coin",
        }

    @classmethod
    async def preflight(cls, creds, symbol, quantity, leverage):
        try:
            instr = await _instruments()
            sym = _norm_symbol(symbol)
            info = instr.get(sym)
            if not info:
                return {"ok": False, "reason": f"Kraken: no perp market for {symbol}"}
            cs = float(info.get("contract_size") or 1)
            if quantity < cs:
                return {"ok": False, "reason": f"Kraken min qty is {cs} {symbol.upper()} (1 contract)."}
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)[:180]}
