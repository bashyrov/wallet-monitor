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
                logger.info("balance %s wallet=%s usdt=%.2f raw=%s",
                            ex, w.id, balance, bal)
            except Exception as exc:
                logger.warning("Balance fetch FAILED for %s wallet=%s uid=%s: %s: %s",
                               ex, w.id, user_id, type(exc).__name__, exc)
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
                    logger.info("balance %s (portfolio-fallback) wallet=%s usdt=%.2f",
                                ex, any_w.id, balance)
                except Exception as exc:
                    logger.warning("Portfolio-fallback balance fetch FAILED for %s wallet=%s uid=%s: %s: %s",
                                   ex, any_w.id, user_id, type(exc).__name__, exc)

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

    # Plan-based trade delay: free tier orders sleep `trade_delay_ms` before
    # signing. Configurable per plan in DB (free=500ms, paid=0ms).
    from backend.db.models import User as _User
    from backend.services import plan_service as _ps
    _user = db.query(_User).filter(_User.id == user_id).first()
    if _user is not None:
        _limits = _ps.effective_limits(db, _user)
        if _limits.trade_delay_ms > 0:
            await asyncio.sleep(_limits.trade_delay_ms / 1000.0)

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

    # Apply the same plan-based trade_delay_ms as place_open_order — without
    # this a Free user could close instantly even though their open path is
    # throttled, which defeats the latency tier.
    from backend.db.models import User as _User
    from backend.services import plan_service as _ps
    _user = db.query(_User).filter(_User.id == user_id).first()
    if _user is not None:
        _limits = _ps.effective_limits(db, _user)
        if _limits.trade_delay_ms > 0:
            await asyncio.sleep(_limits.trade_delay_ms / 1000.0)

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

    # Skip exchanges that don't list this symbol — querying anyway just
    # generates "Instrument ID … doesn't exist" noise (OKX 51001 / Binance
    # -1121 / etc.) on every poll.
    symbol_supported: dict[str, bool] = {}
    if symbol:
        try:
            from backend.services.arbitrage_service import _cache as _arb_cache
            for ex_name, (rows, _ts) in _arb_cache.items():
                if any(r.get("symbol") == symbol for r in rows):
                    symbol_supported[ex_name] = True
        except Exception:
            pass

    async def _one(w: Wallet) -> list[dict]:
        if symbol and symbol_supported and not symbol_supported.get(w.type_value):
            return []
        try:
            creds = decrypt_credentials(w.credentials or {})
            rows = await ADAPTERS[w.type_value].list_positions(creds, symbol)
            for r in rows:
                r["wallet_id"] = w.id
            return rows
        except Exception as exc:
            msg = str(exc)
            # Quiet a few known "symbol doesn't exist on this venue" errors —
            # they're expected when polling a pair across all user wallets.
            if any(s in msg for s in ("51001", "-1121", "Instrument ID", "Invalid symbol")):
                logger.debug("list_positions skipped wallet=%s ex=%s: %s", w.id, w.type_value, msg)
            else:
                logger.info("list_positions failed wallet=%s ex=%s: %s", w.id, w.type_value, exc)
            return []

    results = await asyncio.gather(*(_one(w) for w in wallets), return_exceptions=True)
    flat: list[dict] = []
    for r in results:
        if isinstance(r, list):
            flat.extend(r)
    return flat


async def list_user_orders(db: Session, user_id: int, *, limit: int = 50,
                           symbol: str | None = None) -> list[dict]:
    """Recent trade fills across the user's screener-purpose wallets.

    Reuses transaction_service.fetch_transactions (which already wires up
    per-adapter endpoints for transactions/fills) and filters to the
    `trade` / `fill` event types — deposits and withdrawals are out of
    scope for an "order history" tab. Returns up to `limit` rows sorted
    by timestamp desc, optionally filtered to a single symbol."""
    from backend.services.transaction_service import fetch_transactions
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

    sym_norm = (symbol or "").upper().strip() or None

    async def _one(w: Wallet) -> list[dict]:
        try:
            resp = await fetch_transactions(w)
        except Exception as exc:
            logger.info("list_orders failed wallet=%s ex=%s: %s", w.id, w.type_value, exc)
            return []
        out: list[dict] = []
        for t in (getattr(resp, "transactions", None) or []):
            t_type = (getattr(t, "type", "") or "").lower()
            if t_type not in ("trade", "fill"):
                continue
            asset = (getattr(t, "asset", "") or "").upper()
            if sym_norm and sym_norm not in asset:
                continue
            out.append({
                "wallet_id":  w.id,
                "exchange":   w.type_value,
                "wallet_name": w.name,
                "tx_id":      getattr(t, "tx_id", None),
                "type":       t_type,
                "asset":      asset,
                "amount":     getattr(t, "amount", None),
                "timestamp":  getattr(t, "timestamp", None),
                "status":     getattr(t, "status", None),
                "address":    getattr(t, "address", None),
            })
        return out

    results = await asyncio.gather(*(_one(w) for w in wallets), return_exceptions=True)
    flat: list[dict] = []
    for r in results:
        if isinstance(r, list):
            flat.extend(r)
    # Sort desc by timestamp string — ISO-ish so lexical sort works for
    # the "YYYY-MM-DD HH:MM" format used in transaction_service.
    flat.sort(key=lambda x: (x.get("timestamp") or ""), reverse=True)
    return flat[: max(1, min(int(limit), 500))]


async def list_user_balances(db: Session, user_id: int) -> list[dict]:
    """USDT balance across every screener-purpose exchange wallet the user
    has connected. Returns one row per wallet so the /arb Balances tab can
    render them grouped by exchange. Portfolio-only wallets are explicitly
    excluded — the trading panel cares about KEYS that can place orders or
    are at least screener-attached, not read-only portfolio addresses."""
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

    async def _one(w: Wallet) -> dict:
        ex = (w.type_value or "").lower()
        out = {
            "wallet_id":       w.id,
            "exchange":        ex,
            "name":            w.name or ex,
            "purpose":         w.purpose,
            "can_trade":       bool(getattr(w, "can_trade", False)) or w.purpose in ("screener", "both"),
            "is_main":         bool(getattr(w, "is_main", False)),
            "balance_usdt":    None,
            "error":           None,
        }
        if ex not in ADAPTERS:
            out["error"] = "unsupported"
            return out
        try:
            creds = decrypt_credentials(w.credentials or {})
            bal = await ADAPTERS[ex].fetch_balance(creds)
        except Exception as exc:
            out["error"] = str(exc)[:80]
            logger.info("list_balances failed wallet=%s ex=%s: %s", w.id, ex, exc)
            return out
        # Trade adapters return {"usdt": <float>, ...} (flat). Wallet
        # providers return {"USDT": {"free": ..., "total": ...}, ...} via
        # _build_result. Accept both. The `or`-chain trick is unsafe here
        # — a literal 0.0 is falsy and would skip a real-but-zero balance —
        # so we test each key with explicit `is not None`.
        usdt = None
        if isinstance(bal, dict):
            for key in ("USDT", "usdt", "usdt_balance"):
                if key in bal and bal[key] is not None:
                    entry = bal[key]
                    if isinstance(entry, dict):
                        usdt = entry.get("free")
                        if usdt is None:
                            usdt = entry.get("total")
                    else:
                        try: usdt = float(entry)
                        except (TypeError, ValueError): usdt = None
                    if usdt is not None:
                        break
            # Adapter that returns flat {"available": .., "equity": ..} (no
            # USDT key) — accept too. Used by some early adapter shapes.
            if usdt is None:
                v = bal.get("available")
                if v is None: v = bal.get("free")
                if v is None: v = bal.get("equity")
                try: usdt = float(v) if v is not None else None
                except (TypeError, ValueError): usdt = None
        try:
            out["balance_usdt"] = round(float(usdt), 2) if usdt is not None else None
        except (TypeError, ValueError):
            out["balance_usdt"] = None
        return out

    results = await asyncio.gather(*(_one(w) for w in wallets), return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]
