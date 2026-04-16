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


def _leg_status(wallet: Wallet | None) -> str:
    if wallet is None:
        return "missing"
    if not wallet.can_trade:
        return "disabled"
    return "ok"


async def get_pair_status(db: Session, user_id: int, symbol: str, long_ex: str, short_ex: str) -> dict:
    """Per-leg trading readiness for an arb pair.
    Returns: { long: {wallet_id, status, balance_usdt}, short: {...} }
    """
    out = {"symbol": symbol, "long": {}, "short": {}}
    for leg, ex in (("long", long_ex), ("short", short_ex)):
        if ex not in SUPPORTED_EXCHANGES:
            out[leg] = {"wallet_id": None, "status": "missing", "balance_usdt": None,
                        "note": f"{ex} trading not yet supported"}
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
    if not w.can_trade:
        raise ValueError("Trading not enabled on this wallet")
    ex = (w.type_value or "").lower()
    if ex not in SUPPORTED_EXCHANGES:
        raise ValueError(f"{ex} not supported yet")

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

    try:
        await adapter.set_leverage(creds, symbol, leverage, margin_mode)
    except Exception as exc:
        logger.warning("set_leverage failed %s/%s: %s", ex, symbol, exc)
        # Non-fatal — some accounts already have the desired setup

    result = await adapter.place_order(creds, symbol, side, quantity)
    logger.info("Order placed: user=%s wallet=%s ex=%s sym=%s side=%s qty=%s",
                user_id, wallet_id, ex, symbol, side, quantity)
    return {**result, "exchange": ex, "symbol": symbol, "side": side, "quantity": quantity}


async def close_position(
    db: Session, user_id: int, wallet_id: int, symbol: str, side: str | None = None,
) -> dict:
    w = db.query(Wallet).filter(Wallet.id == wallet_id, Wallet.user_id == user_id).first()
    if not w:
        raise ValueError("Wallet not found")
    if not w.can_trade:
        raise ValueError("Trading not enabled on this wallet")
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
            Wallet.can_trade == True,   # noqa: E712
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
