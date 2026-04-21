"""Unified trade dispatcher — resolves user's wallet for a given exchange and
delegates to the per-exchange adapter.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.orm import Session

from backend.crypto import decrypt_credentials
from backend.db.models import Wallet
from backend.services.trade_adapters import ADAPTERS, SUPPORTED_EXCHANGES

logger = logging.getLogger("avalant.trade")


def _find_wallet(db: Session, user_id: int, exchange: str) -> Wallet | None:
    """Return the user's screener-purpose wallet for an exchange (at most one by design)."""
    return (
        db.query(Wallet)
        .filter(
            Wallet.user_id == user_id,
            Wallet.wallet_type == "exchange",
            Wallet.type_value == exchange.lower(),
            Wallet.purpose.in_(("screener", "both")),
            Wallet.is_archived == False,  # noqa: E712
        )
        .order_by(Wallet.id.desc())
        .first()
    )


def _leg_status(wallet: Wallet | None) -> str:
    if wallet is None:
        return "missing"
    if wallet.purpose not in ("screener", "both"):
        return "disabled"
    return "ok"


def _find_any_wallet(db: Session, user_id: int, exchange: str) -> Wallet | None:
    """Any (non-archived) exchange wallet for this user + exchange, regardless
    of purpose. Used for balance display when a screener key isn't configured
    but a portfolio-only key is — the user still wants to see their balance."""
    return (
        db.query(Wallet)
        .filter(
            Wallet.user_id == user_id,
            Wallet.wallet_type == "exchange",
            Wallet.type_value == exchange.lower(),
            Wallet.is_archived == False,  # noqa: E712
        )
        .order_by(Wallet.id.desc())
        .first()
    )


async def get_pair_status(db: Session, user_id: int, symbol: str, long_ex: str, short_ex: str) -> dict:
    """Per-leg trading readiness for an arb pair.
    Returns: { long: {wallet_id, status, balance_usdt}, short: {...} }
    """
    from backend.services import admin_settings
    trade_blocked = admin_settings.get_trade_disabled_exchanges()
    out = {"symbol": symbol, "long": {}, "short": {}}
    for leg, ex in (("long", long_ex), ("short", short_ex)):
        if ex not in SUPPORTED_EXCHANGES:
            out[leg] = {"wallet_id": None, "status": "missing", "balance_usdt": None,
                        "note": f"{ex} trading not yet supported"}
            continue
        if ex in trade_blocked:
            # Admin blocked, but still try to show balance from any key the
            # user has so they see their funds while we figure out the pause.
            balance = None
            any_w = _find_any_wallet(db, user_id, ex)
            if any_w is not None:
                try:
                    creds = decrypt_credentials(any_w.credentials or {})
                    bal = await ADAPTERS[ex].fetch_balance(creds)
                    balance = round(float(bal.get("usdt", 0) or 0), 2)
                except Exception as exc:
                    logger.info("Balance fetch (admin_blocked) failed for %s: %s", ex, exc)
            out[leg] = {"wallet_id": None, "status": "admin_blocked",
                        "balance_usdt": balance, "exchange": ex,
                        "note": f"Trading on {ex} is temporarily disabled by admin"}
            continue

        w = _find_wallet(db, user_id, ex)
        status = _leg_status(w)
        balance = None
        if status == "ok" and w is not None:
            try:
                creds = decrypt_credentials(w.credentials or {})
                bal = await ADAPTERS[ex].fetch_balance(creds)
                balance = round(float(bal.get("usdt", 0) or 0), 2)
            except Exception as exc:
                logger.info("Balance fetch failed for %s wallet %s: %s", ex, w.id, exc)
                status = "disabled"

        # Even when there's no screener-eligible key, try to show the balance
        # from any portfolio key the user has on this exchange. Trading stays
        # gated on status == "ok"; this is just for display so the balance
        # panel isn't blank when the user flips to an exchange they connected
        # only for Portfolio.
        if status in ("missing", "disabled") and balance is None:
            any_w = _find_any_wallet(db, user_id, ex)
            if any_w is not None and (w is None or any_w.id != w.id):
                try:
                    creds = decrypt_credentials(any_w.credentials or {})
                    bal = await ADAPTERS[ex].fetch_balance(creds)
                    balance = round(float(bal.get("usdt", 0) or 0), 2)
                except Exception as exc:
                    logger.info("Portfolio-fallback balance fetch failed for %s wallet %s: %s",
                                ex, any_w.id, exc)

        out[leg] = {
            "wallet_id": w.id if w else None,
            "status": status,
            "balance_usdt": balance,
            "exchange": ex,
        }
    return out


async def place_open_order(
    db: Session, user_id: int,
    wallet_id: int, symbol: str, side: str, quantity: float,
    leverage: int, margin_mode: str,
) -> dict:
    # Normalise inputs
    symbol = (symbol or "").strip().upper()
    if not symbol or not symbol.isalnum() or len(symbol) > 16:
        raise ValueError(f"Invalid symbol: {symbol!r}")
    if side not in ("buy", "sell"):
        raise ValueError(f"Invalid side: {side!r}")
    if margin_mode not in ("isolated", "cross"):
        raise ValueError(f"Invalid margin_mode: {margin_mode!r}")
    if quantity <= 0:
        raise ValueError("quantity must be > 0")

    w = db.query(Wallet).filter(Wallet.id == wallet_id, Wallet.user_id == user_id).first()
    if not w:
        raise ValueError("Wallet not found")
    if w.purpose not in ("screener", "both"):
        raise ValueError("This wallet is configured for Portfolio (read-only). Enable Screener on it or create a trading key.")
    ex = (w.type_value or "").lower()
    if ex not in SUPPORTED_EXCHANGES:
        raise ValueError(f"{ex} not supported yet")

    # Admin-configured trade block — the exchange still serves screener /
    # funding / portfolio, but new position opens are refused from our
    # side (e.g. during a maintenance window or an integration audit).
    from backend.services import admin_settings
    if ex in admin_settings.get_trade_disabled_exchanges():
        raise ValueError(f"Trading on {ex} is temporarily disabled by admin")

    adapter = ADAPTERS[ex]

    # Clamp leverage to the exchange's real public max so we don't push the user
    # into an order that will be rejected post-signing.
    try:
        if hasattr(adapter, "get_public_max_leverage"):
            max_lev = await adapter.get_public_max_leverage(symbol)
            if max_lev and leverage > max_lev:
                raise ValueError(f"Leverage {leverage}× exceeds {ex} max {max_lev}× for {symbol}")
    except ValueError:
        raise
    except Exception as exc:
        logger.info("max-leverage probe failed %s/%s: %s", ex, symbol, exc)

    creds = decrypt_credentials(w.credentials or {})

    # ── Pre-flight: round qty, validate notional + balance BEFORE signing an order ──
    # Run preflight concurrently with set_leverage when the cache says leverage
    # already matches — in that case we can skip the leverage call entirely.
    from backend.services.trade_adapters import _state_cache

    async def _ensure_leverage() -> None:
        """Skip set_leverage if we applied the same (leverage, margin_mode)
        recently for this account+symbol. Normal flow: user opens 3-5 arb
        legs on the same symbol in quick succession — only the first one
        actually hits the exchange's set-leverage endpoint."""
        if _state_cache.matches(ex, creds, symbol, leverage, margin_mode):
            return
        try:
            await adapter.set_leverage(creds, symbol, leverage, margin_mode)
            _state_cache.record(ex, creds, symbol, leverage, margin_mode)
        except Exception as exc:
            logger.error("set_leverage failed %s/%s lev=%s mode=%s: %s: %s",
                         ex, symbol, leverage, margin_mode,
                         type(exc).__name__, exc)
            # Non-fatal — cached setup may already be correct on the exchange
            # side even if our state-cache was invalidated. The order will
            # either succeed (exchange agrees) or fail with a specific error
            # we surface to the user below.

    preflight_task = None
    if hasattr(adapter, "preflight"):
        preflight_task = asyncio.create_task(adapter.preflight(creds, symbol, quantity, leverage))

    # Always start leverage config in parallel with preflight.
    leverage_task = asyncio.create_task(_ensure_leverage())

    if preflight_task:
        try:
            pre = await preflight_task
            if not pre.get("ok"):
                # Cancel the leverage task so we don't leave it pending if we
                # bail early on a bad preflight.
                leverage_task.cancel()
                raise ValueError(pre.get("reason") or "Pre-flight check failed")
            if pre.get("qty_rounded"):
                quantity = float(pre["qty_rounded"])
        except ValueError:
            raise
        except Exception as exc:
            logger.info("preflight unexpected error %s/%s: %s", ex, symbol, exc)

    await leverage_task

    try:
        result = await adapter.place_order(creds, symbol, side, quantity,
                                           leverage=leverage, margin_mode=margin_mode)
    except RuntimeError as exc:
        # On a failed order, invalidate the state cache — the exchange may
        # have returned a leverage/margin-mode mismatch, and we want the
        # next attempt to re-sync.
        _state_cache.invalidate(ex, creds, symbol)
        # Adapters surface friendly messages in RuntimeError
        raise ValueError(str(exc))
    logger.info("Order placed: user=%s wallet=%s ex=%s sym=%s side=%s qty=%s lev=%sx mode=%s",
                user_id, wallet_id, ex, symbol, side, quantity, leverage, margin_mode)
    return {**result, "exchange": ex, "symbol": symbol, "side": side, "quantity": quantity}


async def close_position(
    db: Session, user_id: int, wallet_id: int, symbol: str, side: str | None = None,
) -> dict:
    w = db.query(Wallet).filter(Wallet.id == wallet_id, Wallet.user_id == user_id).first()
    if not w:
        raise ValueError("Wallet not found")
    if w.purpose not in ("screener", "both"):
        raise ValueError("This wallet is configured for Portfolio (read-only). Enable Screener on it or create a trading key.")
    ex = w.type_value
    if ex not in SUPPORTED_EXCHANGES:
        raise ValueError(f"{ex} not supported yet")

    creds = decrypt_credentials(w.credentials or {})
    return await ADAPTERS[ex].close_position(creds, symbol, side or "")


async def list_user_positions(db: Session, user_id: int, symbol: str | None = None) -> list[dict]:
    """Aggregate open positions across all the user's trade-enabled wallets."""
    wallets = (
        db.query(Wallet)
        .filter(
            Wallet.user_id == user_id,
            Wallet.wallet_type == "exchange",
            Wallet.purpose.in_(("screener", "both")),
            Wallet.is_archived == False,  # noqa: E712
            Wallet.type_value.in_(list(SUPPORTED_EXCHANGES)),
        )
        .all()
    )

    async def _one(w: Wallet) -> list[dict]:
        try:
            creds = decrypt_credentials(w.credentials or {})
            rows = await ADAPTERS[w.type_value].list_positions(creds, symbol)
            for r in rows:
                r["wallet_id"] = w.id
            return rows
        except Exception as exc:
            logger.info("list_positions failed wallet=%s ex=%s: %s", w.id, w.type_value, exc)
            return []

    results = await asyncio.gather(*(_one(w) for w in wallets), return_exceptions=True)
    flat: list[dict] = []
    for r in results:
        if isinstance(r, list):
            flat.extend(r)
    return flat
