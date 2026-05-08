"""Paradex trade adapter — thin Python proxy to go-fetcher.

Paradex signing is SNIP-12 typed-data over the Stark curve. There is no
working Python SDK on Python 3.13 (paradex-py pins starknet-py 0.28
which doesn't load on 3.13). Rather than vendor a Stark crypto stack
into Python we delegate every signing op to go-fetcher's already-tested
adapter at /internal/trade/*.

Credentials map (stored on Wallet.credentials):

    address                 → L2 main account address (0x… felt)
    private_key             → Stark private key (main OR subkey)
    api_passphrase          → (optional) subkey public key (0x…) — when
                              set, Go routes auth through /v1/auth/{pubkey}
    api_token               → (legacy) JWT from paradex.trade UI; only
                              used by the read-only balance provider for
                              Portfolio-only wallets.

For the trade-engine path we translate to the canonical Go shape
(api_key / api_secret / api_passphrase) inside trade_proxy._strip_creds.

place_order / close_position raise KindUser if invoked locally — these
should *never* hit the Python adapter when paradex is in
GO_TRADE_VENUES; they only get called on a Go fallback, and we don't
want to silently sign with the wrong scheme.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("avalant.trade.paradex")


def _need_trade_creds(creds: dict) -> str | None:
    """Return user-facing error if creds aren't sufficient for trading."""
    if not (creds.get("address") or creds.get("api_key")):
        return "Paradex address missing — re-add your wallet with the L2 account address."
    if not (creds.get("private_key") or creds.get("api_secret")):
        return "Paradex private key missing — paste your Stark L2 private key (or a subkey)."
    return None


def _public_specs_lookup(symbol: str) -> dict | None:
    """Hit Paradex /v1/markets via the helper in readonly.py — no auth."""
    import asyncio
    from backend.services.trade_adapters.readonly import _paradex_specs
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    # Already inside an event loop — caller awaits.
    return None  # placeholder; real impl below uses asyncio call


class ParadexAdapter:
    """Lightweight adapter — defers signing/trading to go-fetcher."""

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        # Prefer the Go path when full creds are present (always-fresh
        # balance via SNIP-12). Fall back to legacy JWT-only provider
        # ONLY if a JWT was actually supplied — otherwise re-raise the
        # network/auth error with full context so the user sees the real
        # cause instead of a misleading "missing api_token" message.
        has_trade_creds = not _need_trade_creds(creds)
        if has_trade_creds:
            from backend.services import trade_proxy
            try:
                bal = await trade_proxy.fetch_balance("paradex", creds)
                return {"usdt": float(bal.get("total") or bal.get("total_usd") or bal.get("usdt") or 0)}
            except trade_proxy.GoTradeError as e:
                if not creds.get("api_token"):
                    logger.warning("paradex go fetch_balance failed (no JWT fallback available): %s", e)
                    raise RuntimeError(f"Paradex balance fetch via Go failed: {e}") from e
                logger.info("paradex go fetch_balance failed, trying legacy JWT: %s", e)
        # Legacy JWT path (only reachable when api_token is set)
        token = creds.get("api_token")
        addr  = creds.get("address") or creds.get("api_key")
        if not token or not addr:
            raise RuntimeError("Paradex requires either l2_private_key (trade) or api_token (read-only).")
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api.prod.paradex.trade/v1/balance",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            if r.status_code >= 400:
                raise RuntimeError(f"Paradex {r.status_code}: {r.text[:200]}")
            data = r.json() or {}
        total = 0.0
        for item in (data.get("results") or []):
            if str(item.get("token", "")).upper() != "USDC":
                continue
            try:
                total += float(item.get("size") or 0)
            except (TypeError, ValueError):
                pass
        return {"usdt": total}

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        # If trade is required, verify the full SNIP-12 path against Go.
        if need_trade:
            err = _need_trade_creds(creds)
            if err:
                out["error"] = err
                return out
            from backend.services import trade_proxy
            if not trade_proxy.is_enabled("paradex"):
                out["error"] = "Paradex trade engine is disabled (set GO_TRADE_VENUES). Add this key for Portfolio only or contact admin."
                return out
            try:
                bal = await trade_proxy.fetch_balance("paradex", creds)
                out["can_read"] = True
                out["can_trade"] = True
                out["balance_usdt"] = float(bal.get("total_usd") or bal.get("usdt") or 0)
                return out
            except trade_proxy.GoTradeError as e:
                msg = str(e)
                if "401" in msg or "403" in msg or "Unauthorized" in msg:
                    out["error"] = "Paradex rejected the signature — check L2 address + private key (and subkey public key if used)."
                else:
                    out["error"] = f"Paradex: {msg[:180]}"
                return out
        # Read-only path: prefer trade creds → JWT fallback.
        try:
            bal = await cls.fetch_balance(creds)
            out["can_read"] = True
            out["balance_usdt"] = float(bal.get("usdt") or 0)
        except Exception as e:
            out["error"] = f"Paradex: {str(e)[:180]}"
        return out

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int, margin_mode: str) -> None:
        from backend.services import trade_proxy
        if trade_proxy.is_enabled("paradex"):
            try:
                await trade_proxy.set_leverage("paradex", creds, symbol, leverage, margin_mode)
                return
            except trade_proxy.GoTradeError as e:
                # Don't fail open — but don't loud-fail either; trade_service
                # logs set_leverage errors as non-fatal.
                logger.info("paradex go set_leverage failed: %s", e)
                return
        # Without Go we can't sign. Silently no-op so trade_service flows on
        # to the place_order step which will surface a clearer error.
        return

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        from backend.services import trade_proxy
        if trade_proxy.is_enabled("paradex") and not _need_trade_creds(creds):
            try:
                return await trade_proxy.list_positions("paradex", creds, symbol)
            except trade_proxy.GoTradeError as e:
                logger.info("paradex go list_positions failed: %s", e)
        return []

    @classmethod
    async def preflight(cls, creds: dict, symbol: str, quantity: float, leverage: int) -> dict:
        # Lightweight client-side rounding using the public market specs.
        # Go will run the real preflight on its side — this just keeps the
        # UX consistent with other venues (qty rounded to step before signing).
        from backend.services.trade_adapters.readonly import _paradex_specs
        try:
            specs = await _paradex_specs()
        except Exception:
            specs = {}
        info = specs.get(symbol.upper())
        if not info:
            return {"ok": True, "qty_rounded": quantity}
        step = info.get("step")
        min_qty = float(info.get("min_qty") or 0)
        qty_r = quantity
        if step and step > 0:
            import math
            qty_r = math.floor(quantity / step) * step
        if qty_r <= 0 or qty_r < min_qty:
            return {"ok": False, "reason": f"Paradex min qty for {symbol} is {min_qty}."}
        return {"ok": True, "qty_rounded": qty_r, "step_size": step, "min_qty": min_qty}

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        # Per /v1/markets each perp has a `max_leverage` (we saw 50 in the
        # samples; some markets cap at 20). Could be looked up, but Go will
        # reject anything above the real cap so a generous fallback is fine.
        return 50

    @classmethod
    async def get_public_qty_limits(cls, symbol: str) -> dict | None:
        from backend.services.trade_adapters.readonly import _paradex_specs
        try:
            specs = await _paradex_specs()
        except Exception:
            return None
        info = specs.get(symbol.upper())
        if not info:
            return None
        return {
            "min_qty": float(info.get("min_qty") or 0),
            "step":    info.get("step"),
            "min_notional": info.get("min_notional"),
            "max_qty": None,
            "unit": "coin",
        }

    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated") -> dict:
        # The dispatcher in trade_service.py only reaches this method when
        # Go is disabled OR Go errored out. Either way, we can't sign here —
        # return a clear KindUser error so the failure surfaces cleanly.
        raise RuntimeError(
            "Paradex order signing requires the Go trade engine. "
            "Add `paradex` to GO_TRADE_VENUES or check go-fetcher health."
        )

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str) -> dict:
        raise RuntimeError(
            "Paradex order signing requires the Go trade engine. "
            "Add `paradex` to GO_TRADE_VENUES or check go-fetcher health."
        )
