"""Read-only adapter shim.

For exchanges where Avalant doesn't yet have a trading adapter, we still want
users to add API keys for Portfolio (balance tracking). This wrapper reuses
the existing balance provider under `backend/providers/exchanges/` to validate
the key by performing a live balance fetch. Any order-placement call raises a
clear "trading not supported" error.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("avalant.trade.readonly")


def make_readonly_adapter(exchange: str, display_name: str):
    """Return an adapter class that only supports validate_key / fetch_balance.

    Trade-ish methods raise so the service layer surfaces a clean error instead
    of half-placing an order with incomplete logic."""

    class _ReadOnlyAdapter:
        EXCHANGE = exchange
        DISPLAY = display_name

        # ── Helpers ────────────────────────────────────────────────────────
        @classmethod
        def _build_domain_wallet(cls, creds: dict):
            """Minimal ExchangeWallet shim the existing providers can consume."""
            from backend.domain.models import ExchangeWallet
            from backend.domain.enums import ExchangeType
            return ExchangeWallet(
                name=f"_validation:{exchange}",
                exchange=ExchangeType(exchange),
                api_key=str(creds.get("api_key") or ""),
                api_secret=str(creds.get("api_secret") or ""),
                api_passphrase=str(creds.get("api_passphrase") or "") or None,
            )

        @classmethod
        async def _fetch_balance_usdt(cls, creds: dict) -> float:
            from backend.providers.exchanges import EXCHANGE_PROVIDERS
            cls2 = EXCHANGE_PROVIDERS.get(exchange)
            if cls2 is None:
                raise RuntimeError(f"No balance provider registered for {exchange}")
            provider = cls2()
            try:
                w = cls._build_domain_wallet(creds)
                result = await provider.fetch_balance(w)
                # Heuristic: look at stable totals (USDT / USDC)
                totals = (result.totals or {}) if result else {}
                usdt = float(totals.get("USDT", 0) or 0)
                usdc = float(totals.get("USDC", 0) or 0)
                return usdt + usdc
            finally:
                try:
                    await provider.aclose()
                except Exception:
                    pass

        # ── Adapter surface ────────────────────────────────────────────────
        @classmethod
        async def fetch_balance(cls, creds: dict) -> dict:
            total = await cls._fetch_balance_usdt(creds)
            return {"usdt": total}

        @classmethod
        async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
            out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
            if need_trade:
                out["error"] = f"Trading on {display_name} is not yet supported. Add this key for Portfolio only."
                return out
            try:
                total = await cls._fetch_balance_usdt(creds)
                out["can_read"] = True
                out["balance_usdt"] = float(total or 0)
            except Exception as e:
                msg = str(e)
                lower = msg.lower()
                if any(x in lower for x in ("invalid", "signature", "unauthorized", "401", "403")):
                    out["error"] = f"{display_name} rejected the key. Check API key, secret, and (if required) passphrase."
                elif "passphrase" in lower:
                    out["error"] = f"{display_name} requires an API passphrase."
                elif "timeout" in lower or "network" in lower:
                    out["error"] = f"Could not reach {display_name}. Try again in a moment."
                else:
                    out["error"] = f"{display_name} rejected the key: {msg[:180]}"
            return out

        @classmethod
        async def get_public_max_leverage(cls, symbol: str) -> int:
            return 100  # generic fallback; trading path is blocked anyway

        # Trading ops explicitly unsupported
        @classmethod
        async def set_leverage(cls, creds, symbol, leverage, margin_mode):
            raise RuntimeError(f"Trading on {display_name} is not yet supported.")

        @classmethod
        async def place_order(cls, creds, symbol, side, quantity):
            raise RuntimeError(f"Trading on {display_name} is not yet supported.")

        @classmethod
        async def close_position(cls, creds, symbol, side):
            raise RuntimeError(f"Trading on {display_name} is not yet supported.")

        @classmethod
        async def list_positions(cls, creds, symbol=None):
            return []

        @classmethod
        async def preflight(cls, creds, symbol, quantity, leverage):
            return {"ok": False, "reason": f"Trading on {display_name} is not yet supported."}

    _ReadOnlyAdapter.__name__ = f"{display_name.replace(' ', '')}ReadOnlyAdapter"
    _ReadOnlyAdapter.__qualname__ = _ReadOnlyAdapter.__name__
    return _ReadOnlyAdapter
