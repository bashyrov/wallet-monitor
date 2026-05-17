"""Extended (StarkEx perp DEX) — Python proxy adapter.

The actual signing + order placement lives in
`go-fetcher/internal/trade/extended/extended.go`. This Python class exists
only to satisfy the ADAPTERS registry + the validate_key flow so users
can add Extended keys from the wallet form on /portfolio.

Behavior:
- validate_key  → calls Go /internal/trade/balance via trade_proxy.
                  If Go returns ≥0, the key reads. We currently can't
                  prove can_trade without placing a real order, so trade
                  permission is reported as "best-effort" — same caveat
                  as other proxy-only venues.
- fetch_balance → same path, returns {"usdt": <total>}.
- place_order / close_position / set_leverage / list_positions →
                  raise NotImplementedError so the dispatcher routes via
                  trade_proxy when "extended" is in GO_TRADE_VENUES.
                  Without GO_TRADE_VENUES the order never reaches this
                  class because the proxy short-circuits at the
                  is_enabled check.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("avalant.trade.extended")


class ExtendedAdapter:
    EXCHANGE = "extended"
    DISPLAY = "Extended"

    @classmethod
    async def validate_key(cls, creds: dict, need_trade: bool = False) -> dict:
        out = {"can_read": False, "can_trade": False, "balance_usdt": None, "error": None}
        # Quick credential shape check before hitting the network. Balance
        # reads on Extended use only the X-Api-Key header — no Stark
        # signature, no vault, no public key. Those four fields are only
        # required when the user wants to TRADE.
        missing = []
        if not (creds.get("api_key") or "").strip():
            missing.append("api_key")
        if need_trade:
            if not (creds.get("private_key") or creds.get("api_secret") or "").strip():
                missing.append("private_key (Stark L2)")
            if not (creds.get("address") or creds.get("wallet") or "").strip():
                missing.append("address (Stark L2 public key)")
            if not (creds.get("api_passphrase") or creds.get("passphrase") or "").strip():
                missing.append("vault (collateral_position_id)")
        if missing:
            label = "Extended (trading)" if need_trade else "Extended"
            out["error"] = f"{label} requires: " + ", ".join(missing)
            return out

        from backend.services import trade_proxy
        try:
            bal = await trade_proxy.fetch_balance("extended", creds)
            out["can_read"] = True
            out["balance_usdt"] = float(bal.get("usdt") or bal.get("total") or 0)
            if need_trade:
                # We can't prove can_trade without sending a real order. We
                # mark it True if balance reads succeeded — Extended's API
                # key is the same one used for trading, so a working read
                # is a strong indicator. Real trade attempts will surface
                # any permission mismatch.
                out["can_trade"] = True
        except trade_proxy.GoTradeError as e:
            kind = (e.kind or "").lower()
            if kind == "transient":
                out["error"] = "Could not reach Extended (proxy unreachable). Set GO_TRADE_VENUES=...,extended and ensure go-fetcher is running."
            elif kind == "user":
                out["error"] = f"Extended rejected the key: {e.message[:180]}"
            else:
                out["error"] = f"Extended validation failed: {e.message[:180]}"
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"Extended validation failed: {str(exc)[:180]}"
        return out

    @classmethod
    async def fetch_balance(cls, creds: dict) -> dict:
        from backend.services import trade_proxy
        bal = await trade_proxy.fetch_balance("extended", creds)
        total = float(bal.get("usdt") or bal.get("total") or 0)
        # Extended is futures-only (no spot product live yet).
        return {"usdt": total, "spot_usd": 0.0, "futures_usd": total}

    # ── Trade methods — Python fallbacks when GO_TRADE_VENUES doesn't
    # include "extended". When the proxy is enabled it short-circuits
    # before any of these run; with the proxy off, these surface a clean
    # error so the dispatcher reports it instead of crashing.
    @classmethod
    async def place_order(cls, creds: dict, symbol: str, side: str, quantity: float,
                          leverage: int = 1, margin_mode: str = "isolated",
                          market_type: str = "futures",
                          order_type: str = "market",
                          limit_price: float | None = None,
                          stop_price: float | None = None) -> dict:
        # The dispatcher only reaches us if the proxy errored with a
        # transient/internal fault. Delegate to trade_proxy regardless —
        # the user's intent is to trade extended, and the only sign path
        # lives in Go. If proxy is unreachable we surface a clean error.
        from backend.services import trade_proxy
        return await trade_proxy.place_order(
            "extended", creds, symbol, side, quantity,
            leverage=leverage, margin_mode=margin_mode,
            market_type=market_type, order_type=order_type,
            limit_price=limit_price, stop_price=stop_price,
        )

    @classmethod
    async def close_position(cls, creds: dict, symbol: str, side: str | None = None,
                             market_type: str = "futures") -> dict:
        from backend.services import trade_proxy
        return await trade_proxy.close_position("extended", creds, symbol, side or "",
                                                 market_type=market_type)

    @classmethod
    async def set_leverage(cls, creds: dict, symbol: str, leverage: int,
                           margin_mode: str = "isolated") -> None:
        # Extended doesn't have an explicit leverage knob — leverage is
        # derived from position notional / collateral. No-op.
        return None

    @classmethod
    async def list_positions(cls, creds: dict, symbol: str | None = None) -> list[dict]:
        from backend.services import trade_proxy
        return await trade_proxy.list_positions("extended", creds, symbol)

    @classmethod
    async def get_public_qty_limits(cls, symbol: str) -> dict | None:
        # Could be filled from /info/markets but it's not critical for the
        # add-key path — leave None so the UI falls back to generic hints.
        return None

    @classmethod
    async def get_public_max_leverage(cls, symbol: str) -> int:
        # Extended tier-based; reasonable upper bound for the picker.
        return 50
