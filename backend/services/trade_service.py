"""Unified trade dispatcher — resolves user's wallet for a given exchange and
delegates to the per-exchange adapter.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from datetime import datetime

from sqlalchemy.orm import Session

from backend.crypto import decrypt_credentials
from backend.db.models import Wallet, TradeOrder
from backend.services.trade_adapters import ADAPTERS, SUPPORTED_EXCHANGES

logger = logging.getLogger("avalant.trade")


class TradeError(ValueError):
    """Trade-service error with structured metadata.

    `kind`:
      - "user"     : caller's input was rejected by our own validation
                     (bad symbol, leverage too high, etc). Surface verbatim.
      - "exchange" : venue rejected the request. Surface verbatim — the
                     user wants the venue's actual code/message.
      - "internal" : something on our side broke. UI shows a generic
                     "unexpected error — see Order History"; the truth
                     stays in the trade_orders row.
    """

    def __init__(self, message: str, *, kind: str = "exchange",
                 code: str | None = None, raw: dict | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.raw = raw


def _log_order(
    db: Session, *,
    user_id: int, wallet_id: int | None,
    exchange: str, symbol: str, side: str, intent: str,
    requested_qty: float, leverage: int | None = None,
    margin_mode: str | None = None,
    status: str = "pending",
    exchange_order_id: str | None = None,
    filled_qty: float | None = None,
    avg_fill_price: float | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    error_kind: str | None = None,
    raw_response: dict | None = None,
) -> TradeOrder:
    """Insert a trade_orders row and commit. Used for both pending entries
    (right before the upstream call) and finalised entries (when we have a
    one-shot success/failure outcome)."""
    row = TradeOrder(
        user_id=user_id,
        wallet_id=wallet_id,
        exchange=exchange,
        symbol=symbol,
        side=side,
        intent=intent,
        order_type="market",
        requested_qty=float(requested_qty),
        leverage=int(leverage) if leverage is not None else None,
        margin_mode=margin_mode,
        status=status,
        exchange_order_id=exchange_order_id,
        filled_qty=float(filled_qty) if filled_qty is not None else None,
        avg_fill_price=float(avg_fill_price) if avg_fill_price is not None else None,
        error_code=error_code,
        error_message=error_message,
        error_kind=error_kind,
        raw_response=raw_response,
        finalized_at=datetime.utcnow() if status != "pending" else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _finalize_order(db: Session, order: TradeOrder, *, status: str,
                    exchange_order_id: str | None = None,
                    filled_qty: float | None = None,
                    avg_fill_price: float | None = None,
                    error_code: str | None = None,
                    error_message: str | None = None,
                    error_kind: str | None = None,
                    raw_response: dict | None = None) -> None:
    order.status = status
    if exchange_order_id is not None:
        order.exchange_order_id = exchange_order_id
    if filled_qty is not None:
        order.filled_qty = float(filled_qty)
    if avg_fill_price is not None:
        order.avg_fill_price = float(avg_fill_price)
    if error_code is not None:
        order.error_code = error_code
    if error_message is not None:
        order.error_message = error_message
    if error_kind is not None:
        order.error_kind = error_kind
    if raw_response is not None:
        order.raw_response = raw_response
    order.finalized_at = datetime.utcnow()
    db.commit()


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
        raise TradeError(f"Invalid symbol: {symbol!r}", kind="user")
    if side not in ("buy", "sell"):
        raise TradeError(f"Invalid side: {side!r}", kind="user")
    if margin_mode not in ("isolated", "cross"):
        raise TradeError(f"Invalid margin_mode: {margin_mode!r}", kind="user")
    if quantity <= 0:
        raise TradeError("quantity must be > 0", kind="user")

    w = db.query(Wallet).filter(Wallet.id == wallet_id, Wallet.user_id == user_id).first()
    if not w:
        raise TradeError("Wallet not found", kind="user")
    if w.purpose not in ("screener", "both"):
        raise TradeError("This wallet is configured for Portfolio (read-only). Enable Screener on it or create a trading key.", kind="user")
    ex = (w.type_value or "").lower()
    if ex not in SUPPORTED_EXCHANGES:
        raise TradeError(f"{ex} not supported yet", kind="user")

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
        raise TradeError(f"Trading on {ex} is temporarily disabled by admin", kind="user")

    adapter = ADAPTERS[ex]

    # Clamp leverage to the exchange's real public max so we don't push the user
    # into an order that will be rejected post-signing.
    try:
        if hasattr(adapter, "get_public_max_leverage"):
            max_lev = await adapter.get_public_max_leverage(symbol)
            if max_lev and leverage > max_lev:
                raise TradeError(f"Leverage {leverage}× exceeds {ex} max {max_lev}× for {symbol}", kind="user")
    except TradeError:
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
                raise TradeError(pre.get("reason") or "Pre-flight check failed", kind="user")
            if pre.get("qty_rounded"):
                quantity = float(pre["qty_rounded"])
        except TradeError:
            raise
        except Exception as exc:
            logger.info("preflight unexpected error %s/%s: %s", ex, symbol, exc)

    await leverage_task

    # Log the pending row before signing — gives us a permanent record even
    # if our process crashes mid-flight.
    order_row = _log_order(
        db, user_id=user_id, wallet_id=wallet_id, exchange=ex, symbol=symbol,
        side=side, intent="open", requested_qty=quantity, leverage=leverage,
        margin_mode=margin_mode, status="pending",
    )

    try:
        result = await adapter.place_order(creds, symbol, side, quantity,
                                           leverage=leverage, margin_mode=margin_mode)
    except RuntimeError as exc:
        # Exchange rejected the order. Surface its message verbatim — that's
        # what the user wants to see — but also persist it.
        _state_cache.invalidate(ex, creds, symbol)
        msg = str(exc)
        _finalize_order(db, order_row, status="failed", error_kind="exchange",
                        error_message=msg)
        logger.warning(
            "Order REJECTED by exchange: user=%s wallet=%s ex=%s sym=%s side=%s qty=%s order_db_id=%s err=%s",
            user_id, wallet_id, ex, symbol, side, quantity, order_row.id, msg,
        )
        raise TradeError(msg, kind="exchange")
    except Exception as exc:
        # Anything else is on us.
        _state_cache.invalidate(ex, creds, symbol)
        logger.exception(
            "Order FAILED (internal) user=%s wallet=%s ex=%s sym=%s side=%s qty=%s order_db_id=%s",
            user_id, wallet_id, ex, symbol, side, quantity, order_row.id,
        )
        _finalize_order(db, order_row, status="failed", error_kind="internal",
                        error_message=f"{type(exc).__name__}: {exc}")
        raise TradeError("unexpected error — see Order History", kind="internal")

    fill_price = result.get("avg_price") or result.get("fill_price") or result.get("price")
    fill_qty = result.get("filled_qty") or result.get("qty") or quantity
    _finalize_order(
        db, order_row, status="filled",
        exchange_order_id=str(result.get("order_id") or "") or None,
        filled_qty=fill_qty,
        avg_fill_price=float(fill_price) if fill_price else None,
        raw_response=result if isinstance(result, dict) else None,
    )
    logger.info(
        "Order PLACED: user=%s wallet=%s ex=%s sym=%s side=%s qty=%s lev=%sx mode=%s order_db_id=%s ex_order_id=%s",
        user_id, wallet_id, ex, symbol, side, quantity, leverage, margin_mode,
        order_row.id, result.get("order_id"),
    )
    invalidate_positions_cache(user_id)
    return {**result, "exchange": ex, "symbol": symbol, "side": side, "quantity": quantity,
            "order_db_id": order_row.id}


async def close_position(
    db: Session, user_id: int, wallet_id: int, symbol: str, side: str | None = None,
) -> dict:
    w = db.query(Wallet).filter(Wallet.id == wallet_id, Wallet.user_id == user_id).first()
    if not w:
        raise TradeError("Wallet not found", kind="user")
    if w.purpose not in ("screener", "both"):
        raise TradeError("This wallet is configured for Portfolio (read-only). Enable Screener on it or create a trading key.", kind="user")
    ex = w.type_value
    if ex not in SUPPORTED_EXCHANGES:
        raise TradeError(f"{ex} not supported yet", kind="user")

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

    norm_symbol = (symbol or "").strip().upper()
    close_side = (side or "").strip().lower() or "sell"
    order_row = _log_order(
        db, user_id=user_id, wallet_id=wallet_id, exchange=ex, symbol=norm_symbol,
        side=close_side, intent="close", requested_qty=0.0, status="pending",
    )

    try:
        result = await ADAPTERS[ex].close_position(creds, symbol, side or "")
    except RuntimeError as exc:
        msg = str(exc)
        _finalize_order(db, order_row, status="failed", error_kind="exchange",
                        error_message=msg)
        logger.warning(
            "Close REJECTED by exchange: user=%s wallet=%s ex=%s sym=%s order_db_id=%s err=%s",
            user_id, wallet_id, ex, symbol, order_row.id, msg,
        )
        raise TradeError(msg, kind="exchange")
    except Exception as exc:
        logger.exception(
            "Close FAILED (internal) user=%s wallet=%s ex=%s sym=%s order_db_id=%s",
            user_id, wallet_id, ex, symbol, order_row.id,
        )
        _finalize_order(db, order_row, status="failed", error_kind="internal",
                        error_message=f"{type(exc).__name__}: {exc}")
        raise TradeError("unexpected error — see Order History", kind="internal")

    fill_price = (result or {}).get("avg_price") or (result or {}).get("price")
    fill_qty = (result or {}).get("filled_qty") or (result or {}).get("qty")
    _finalize_order(
        db, order_row, status="filled",
        exchange_order_id=str((result or {}).get("order_id") or "") or None,
        filled_qty=fill_qty,
        avg_fill_price=float(fill_price) if fill_price else None,
        raw_response=result if isinstance(result, dict) else None,
    )
    logger.info(
        "Close PLACED: user=%s wallet=%s ex=%s sym=%s order_db_id=%s ex_order_id=%s",
        user_id, wallet_id, ex, symbol, order_row.id, (result or {}).get("order_id"),
    )
    invalidate_positions_cache(user_id)
    return result


# In-memory cache for list_user_positions: collapses repeat hits within
# the TTL window into a single set of upstream API calls. Eight wallets ×
# ~500-2000ms per list_positions = noticeable first-load latency on /arb;
# the cache makes the periodic 8-10s polls effectively free for the second+
# request that comes within TTL_S.
_POSITIONS_CACHE: dict[tuple[int, str], tuple[float, list[dict]]] = {}
# 15s TTL aligns with the 10s browser-poll cadence — every other poll hits
# cache, halving upstream calls. Order/close events explicitly invalidate
# (invalidate_positions_cache), so freshness for the user's own actions
# stays sub-second. Mark price for unrealized-PnL still updates from the
# funding WS feed so the on-screen number isn't stale.
_POSITIONS_CACHE_TTL_S = 15.0

# Per-wallet last-good snapshot. When an upstream call transiently fails
# (rate limit, timeout, etc.) we serve the last successful rows instead of
# dropping to [], otherwise positions blink out of the UI for a few seconds
# until the next poll succeeds. Successful empty results overwrite this so
# legitimately-closed positions do disappear.
_POSITIONS_LASTGOOD: dict[tuple[int, int, str], tuple[float, list[dict]]] = {}
_POSITIONS_LASTGOOD_TTL_S = 30.0


async def list_user_positions(db: Session, user_id: int, symbol: str | None = None) -> list[dict]:
    """Aggregate open positions across all the user's trade-enabled wallets."""
    import time as _time
    cache_key = (user_id, (symbol or "").upper())
    cached = _POSITIONS_CACHE.get(cache_key)
    if cached and (_time.time() - cached[0]) < _POSITIONS_CACHE_TTL_S:
        return cached[1]
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
        lg_key = (user_id, w.id, (symbol or "").upper())
        if symbol and symbol_supported and not symbol_supported.get(w.type_value):
            return []

        # WS user-stream short-circuit: if a live stream exists for this
        # wallet, return its in-memory snapshot instead of hitting the
        # exchange REST API. Status TTL is 60s on Redis — stale = stream
        # broken/down → fall through to REST.
        try:
            from backend.services.user_streams import _snapshot as _us_snapshot
            if _us_snapshot.get_status(user_id, w.id) == "LIVE":
                rows = _us_snapshot.get_positions(user_id, w.id) or []
                if symbol:
                    sym_norm = symbol.upper().strip()
                    rows = [r for r in rows if (r.get("symbol") or "").upper() == sym_norm]
                for r in rows:
                    r["wallet_id"] = w.id
                _POSITIONS_LASTGOOD[lg_key] = (_time.time(), rows)
                return rows
        except Exception as exc:
            # Snapshot read should never throw — log and continue to REST.
            logger.debug("userstream snapshot read failed: %s", exc)

        try:
            creds = decrypt_credentials(w.credentials or {})
            rows = await ADAPTERS[w.type_value].list_positions(creds, symbol)
            for r in rows:
                r["wallet_id"] = w.id
            _POSITIONS_LASTGOOD[lg_key] = (_time.time(), rows)
            return rows
        except Exception as exc:
            msg = str(exc)
            # "Symbol not on this venue" errors are real empties, not blips —
            # don't fall back to last-good for them.
            symbol_not_on_venue = any(s in msg for s in ("51001", "-1121", "Instrument ID", "Invalid symbol"))
            if symbol_not_on_venue:
                logger.debug("list_positions skipped wallet=%s ex=%s: %s", w.id, w.type_value, msg)
                return []
            logger.info("list_positions failed wallet=%s ex=%s: %s", w.id, w.type_value, exc)
            lg = _POSITIONS_LASTGOOD.get(lg_key)
            if lg and (_time.time() - lg[0]) < _POSITIONS_LASTGOOD_TTL_S:
                return lg[1]
            return []

    results = await asyncio.gather(*(_one(w) for w in wallets), return_exceptions=True)
    flat: list[dict] = []
    for r in results:
        if isinstance(r, list):
            flat.extend(r)
    _POSITIONS_CACHE[cache_key] = (_time.time(), flat)
    return flat


def invalidate_positions_cache(user_id: int) -> None:
    """Drop cached positions / balances for a user — called after
    place_order / close so the next poll sees the new state immediately
    rather than waiting out the TTL. Also clears last-good per-wallet rows
    so a freshly-closed position can't be revived by a subsequent fetch
    failure."""
    keys = [k for k in _POSITIONS_CACHE if k[0] == user_id]
    for k in keys:
        _POSITIONS_CACHE.pop(k, None)
    lg_keys = [k for k in _POSITIONS_LASTGOOD if k[0] == user_id]
    for k in lg_keys:
        _POSITIONS_LASTGOOD.pop(k, None)
    _BALANCES_CACHE.pop(user_id, None)


# Balances cache — same shape as positions but keyed by user_id only
# (no symbol filter on /trade/balances). 30s TTL because USDT balances
# don't change often outside of order events, and we explicitly invalidate
# on order placed / closed.
_BALANCES_CACHE: dict[int, tuple[float, list[dict]]] = {}
_BALANCES_CACHE_TTL_S = 30.0


# ── Pair decisions ─────────────────────────────────────────────────────────
# The user's manual Sync ⇆ / Unpair choices are persisted server-side so
# they survive page refresh (previously these lived in localStorage). Each
# decision links two leg-fingerprints. Today the fingerprint is symbol +
# exchange + side; we'll extend with wallet_id once users routinely run
# multiple wallets per venue.

def _pair_legs_for(symbol: str, long_ex: str, short_ex: str) -> tuple[str, str]:
    sym = (symbol or "").upper().strip()
    long_ex = (long_ex or "").lower().strip()
    short_ex = (short_ex or "").lower().strip()
    return f"{sym}|{long_ex}|buy", f"{sym}|{short_ex}|sell"


def set_pair_decision(db: Session, user_id: int, symbol: str,
                       long_exchange: str, short_exchange: str,
                       decision: str) -> None:
    if decision not in ("paired", "unpaired"):
        raise TradeError("Invalid decision", kind="user")
    from backend.db.models import TradePairDecision
    leg_a, leg_b = _pair_legs_for(symbol, long_exchange, short_exchange)
    row = (
        db.query(TradePairDecision)
        .filter(
            TradePairDecision.user_id == user_id,
            TradePairDecision.leg_a_key == leg_a,
            TradePairDecision.leg_b_key == leg_b,
        )
        .first()
    )
    if row:
        row.decision = decision
        row.updated_at = datetime.utcnow()
    else:
        from backend.db.models import TradePairDecision as _TPD
        db.add(_TPD(user_id=user_id, leg_a_key=leg_a, leg_b_key=leg_b, decision=decision))
    db.commit()
    logger.info(
        "Pair decision: user=%s sym=%s long=%s short=%s decision=%s",
        user_id, symbol, long_exchange, short_exchange, decision,
    )


def list_pair_decisions(db: Session, user_id: int) -> list[dict]:
    """Return active (decision != 'unpaired') pair decisions for the user.

    The frontend uses this to pre-populate the manual-pair list, replacing
    the legacy localStorage cache. Unpaired decisions stay in the DB so we
    can avoid re-suggesting the same auto-pair the user has already
    rejected, but we don't surface them to the UI."""
    from backend.db.models import TradePairDecision
    rows = (
        db.query(TradePairDecision)
        .filter(TradePairDecision.user_id == user_id,
                TradePairDecision.decision == "paired")
        .all()
    )
    out: list[dict] = []
    for r in rows:
        # leg_a_key = "SYM|long_ex|buy", leg_b_key = "SYM|short_ex|sell"
        try:
            sym, long_ex, _ = r.leg_a_key.split("|")
            _, short_ex, _ = r.leg_b_key.split("|")
            out.append({"symbol": sym, "long_exchange": long_ex, "short_exchange": short_ex})
        except ValueError:
            continue
    return out


def _serialize_order(o: TradeOrder) -> dict:
    """Shape a TradeOrder row for the Order History UI. Internal errors are
    sanitized to a generic message — the user shouldn't see our stack
    traces, even though the row in the DB still has the truth for support."""
    sanitized_msg = o.error_message
    if o.error_kind == "internal" and sanitized_msg:
        sanitized_msg = "Unexpected error — please contact support if this persists"
    return {
        "id":               o.id,
        "wallet_id":        o.wallet_id,
        "position_id":      o.position_id,
        "exchange":         o.exchange,
        "symbol":           o.symbol,
        "side":             o.side,
        "intent":           o.intent,
        "order_type":       o.order_type,
        "requested_qty":    o.requested_qty,
        "leverage":         o.leverage,
        "margin_mode":      o.margin_mode,
        "status":           o.status,
        "exchange_order_id": o.exchange_order_id,
        "filled_qty":       o.filled_qty,
        "avg_fill_price":   o.avg_fill_price,
        "fee_usd":          o.fee_usd,
        "error_kind":       o.error_kind,
        "error_message":    sanitized_msg,
        "raw_response":     o.raw_response if o.error_kind != "internal" else None,
        "created_at":       o.created_at.isoformat() if o.created_at else None,
        "finalized_at":     o.finalized_at.isoformat() if o.finalized_at else None,
    }


# ── P&L (closed positions) ───────────────────────────────────────────────
def _pnl_pair_decisions(db: Session, user_id: int) -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]]]:
    """Return (paired, unpaired) sets keyed by (symbol, long_ex, short_ex)."""
    from backend.db.models import TradePairDecision
    rows = db.query(TradePairDecision).filter(TradePairDecision.user_id == user_id).all()
    paired: set[tuple[str, str, str]] = set()
    unpaired: set[tuple[str, str, str]] = set()
    for r in rows:
        try:
            sym, long_ex, _ = r.leg_a_key.split("|")
            _, short_ex, _ = r.leg_b_key.split("|")
        except ValueError:
            continue
        key = (sym.upper(), long_ex.lower(), short_ex.lower())
        (paired if r.decision == "paired" else unpaired).add(key)
    return paired, unpaired


def _pnl_can_pair(long_pos, short_pos, paired: set, unpaired: set) -> bool:
    """Apply user decisions then the spread%±5% rule."""
    sym = (long_pos.symbol or "").upper()
    long_ex = (long_pos.leg_a_exchange or "").lower()
    short_ex = (short_pos.leg_a_exchange or "").lower()
    key = (sym, long_ex, short_ex)
    if key in unpaired:
        return False
    if key in paired:
        return True
    # Auto rule: notional diff% within spread%±5%, opened within 5 min.
    le = float(long_pos.leg_a_entry_price or 0)
    se = float(short_pos.leg_a_entry_price or 0)
    if le <= 0 or se <= 0:
        return False
    long_n = float(long_pos.leg_a_qty or 0) * le
    short_n = float(short_pos.leg_a_qty or 0) * se
    max_n = max(long_n, short_n)
    if max_n <= 0:
        return False
    spread_pct = abs((se - le) / le) * 100.0
    diff_pct = abs(long_n - short_n) / max_n * 100.0
    if abs(diff_pct - spread_pct) > 5.0:
        return False
    # 5-minute opening window
    if long_pos.opened_at and short_pos.opened_at:
        delta = abs((long_pos.opened_at - short_pos.opened_at).total_seconds())
        if delta > 5 * 60:
            return False
    return True


def list_user_pnl(db: Session, user_id: int, *, days: int = 30) -> list[dict]:
    """P&L tab — closed positions over the last `days` days, grouped into
    pairs where pair-decision OR auto-detect (spread%±5% / 5-min window)
    applies. Partial-closed pairs (one leg closed, the other still open)
    are filtered out — those still belong in the live Positions tab.
    """
    from backend.db.models import TradePosition
    from datetime import timedelta as _td
    cutoff = datetime.utcnow() - _td(days=int(days))
    paired, unpaired = _pnl_pair_decisions(db, user_id)

    closed = (
        db.query(TradePosition)
        .filter(
            TradePosition.user_id == user_id,
            TradePosition.kind == "single",
            TradePosition.status == "closed",
            TradePosition.closed_at >= cutoff,
        )
        .order_by(TradePosition.closed_at.desc())
        .all()
    )
    open_rows = (
        db.query(TradePosition)
        .filter(
            TradePosition.user_id == user_id,
            TradePosition.kind == "single",
            TradePosition.status == "open",
        )
        .all()
    )

    # Partition closed by symbol+side so we can find counterparts efficiently.
    by_sym_side: dict[tuple[str, str], list] = {}
    for r in closed:
        key = ((r.symbol or "").upper(), (r.leg_a_side or "").lower())
        by_sym_side.setdefault(key, []).append(r)

    open_by_sym_side: dict[tuple[str, str], list] = {}
    for r in open_rows:
        key = ((r.symbol or "").upper(), (r.leg_a_side or "").lower())
        open_by_sym_side.setdefault(key, []).append(r)

    used_ids: set[int] = set()
    out: list[dict] = []

    # First pass: pair up closed singles into pair rows.
    for r in closed:
        if r.id in used_ids:
            continue
        sym = (r.symbol or "").upper()
        side = (r.leg_a_side or "").lower()
        opp_side = "sell" if side == "buy" else "buy"
        candidates = [c for c in by_sym_side.get((sym, opp_side), []) if c.id not in used_ids]

        long_pos, short_pos = (r, None) if side == "buy" else (None, r)
        match = None
        for c in candidates:
            l, s = (r, c) if side == "buy" else (c, r)
            if _pnl_can_pair(l, s, paired, unpaired):
                match = c
                long_pos, short_pos = l, s
                break

        # If we found a pair candidate but its counterpart in OPEN exists
        # for the same symbol+opposite-side combo, this pair is partial —
        # skip both for now.
        if match:
            partner_open = any(
                op.leg_a_exchange == match.leg_a_exchange
                for op in open_by_sym_side.get((sym, opp_side), [])
            )
            this_open = any(
                op.leg_a_exchange == r.leg_a_exchange
                for op in open_by_sym_side.get((sym, side), [])
            )
            if partner_open or this_open:
                used_ids.add(r.id); used_ids.add(match.id)
                continue
            used_ids.add(r.id); used_ids.add(match.id)
            out.append(_serialize_pnl_pair(long_pos, short_pos))
            continue

        # No pair candidate — could still be a partial pair if the
        # opposite side is currently open with a matching pair-decision.
        opp_opens = open_by_sym_side.get((sym, opp_side), [])
        if opp_opens:
            l_open, s_open = (r, opp_opens[0]) if side == "buy" else (opp_opens[0], r)
            if _pnl_can_pair(l_open, s_open, paired, unpaired):
                # Partial pair — counterpart still open. Skip from P&L.
                used_ids.add(r.id)
                continue

        used_ids.add(r.id)
        out.append(_serialize_pnl_single(r))

    return out


def _serialize_pnl_single(r) -> dict:
    return {
        "kind": "single",
        "id": r.id,
        "symbol": r.symbol,
        "exchange": r.leg_a_exchange,
        "side": r.leg_a_side,
        "qty": r.leg_a_qty,
        "entry_price": r.leg_a_entry_price,
        "exit_price": r.leg_a_exit_price,
        "realized_pnl_usd": r.leg_a_realized_pnl_usd,
        "funding_pnl_usd": r.leg_a_funding_pnl_usd,
        "fees_usd": r.leg_a_fees_usd,
        "total_pnl_usd": (r.leg_a_realized_pnl_usd or 0)
                         + (r.leg_a_funding_pnl_usd or 0)
                         - (r.leg_a_fees_usd or 0),
        "opened_at": r.opened_at.isoformat() if r.opened_at else None,
        "closed_at": r.closed_at.isoformat() if r.closed_at else None,
        "opened_externally": bool(r.opened_externally),
        "closed_externally": bool(r.closed_externally),
    }


def _serialize_pnl_pair(long_pos, short_pos) -> dict:
    le = float(long_pos.leg_a_entry_price or 0)
    se = float(short_pos.leg_a_entry_price or 0)
    long_realized = long_pos.leg_a_realized_pnl_usd or 0
    short_realized = short_pos.leg_a_realized_pnl_usd or 0
    long_funding = long_pos.leg_a_funding_pnl_usd or 0
    short_funding = short_pos.leg_a_funding_pnl_usd or 0
    long_fees = long_pos.leg_a_fees_usd or 0
    short_fees = short_pos.leg_a_fees_usd or 0
    total = long_realized + short_realized + long_funding + short_funding - long_fees - short_fees
    spread_pct = abs((se - le) / le) * 100.0 if le > 0 else None
    opened_at = max(filter(None, [long_pos.opened_at, short_pos.opened_at])) if (long_pos.opened_at or short_pos.opened_at) else None
    closed_at = max(filter(None, [long_pos.closed_at, short_pos.closed_at])) if (long_pos.closed_at or short_pos.closed_at) else None
    return {
        "kind": "pair",
        "pair_kind": "long_short",
        "id": f"{long_pos.id}-{short_pos.id}",
        "symbol": long_pos.symbol,
        "long":  {
            "exchange": long_pos.leg_a_exchange,
            "qty": long_pos.leg_a_qty,
            "entry_price": long_pos.leg_a_entry_price,
            "exit_price": long_pos.leg_a_exit_price,
            "realized_pnl_usd": long_realized,
            "funding_pnl_usd": long_funding,
            "fees_usd": long_fees,
            "opened_externally": bool(long_pos.opened_externally),
            "closed_externally": bool(long_pos.closed_externally),
        },
        "short": {
            "exchange": short_pos.leg_a_exchange,
            "qty": short_pos.leg_a_qty,
            "entry_price": short_pos.leg_a_entry_price,
            "exit_price": short_pos.leg_a_exit_price,
            "realized_pnl_usd": short_realized,
            "funding_pnl_usd": short_funding,
            "fees_usd": short_fees,
            "opened_externally": bool(short_pos.opened_externally),
            "closed_externally": bool(short_pos.closed_externally),
        },
        "total_realized_pnl_usd": long_realized + short_realized,
        "total_funding_pnl_usd": long_funding + short_funding,
        "total_fees_usd": long_fees + short_fees,
        "total_pnl_usd": total,
        "entry_spread_pct": spread_pct,
        "opened_at": opened_at.isoformat() if opened_at else None,
        "closed_at": closed_at.isoformat() if closed_at else None,
    }


async def list_user_orders(db: Session, user_id: int, *, limit: int = 50,
                           symbol: str | None = None) -> list[dict]:
    """Order History: every order our service sent to a venue for this user.

    Reads `trade_orders` directly — does NOT include fills the user did on
    the exchange UI itself, by design. Order History is "what we did";
    P&L tab will be the place that aggregates everything.

    Sorted desc by created_at, capped at `limit`.
    """
    sym = (symbol or "").upper().strip() or None
    q = db.query(TradeOrder).filter(TradeOrder.user_id == user_id)
    if sym:
        q = q.filter(TradeOrder.symbol == sym)
    rows = q.order_by(TradeOrder.created_at.desc()).limit(max(1, min(int(limit), 500))).all()
    return [_serialize_order(r) for r in rows]


async def list_user_balances(db: Session, user_id: int) -> list[dict]:
    """USDT balance across every screener-purpose exchange wallet the user
    has connected. Returns one row per wallet so the /arb Balances tab can
    render them grouped by exchange. Portfolio-only wallets are explicitly
    excluded — the trading panel cares about KEYS that can place orders or
    are at least screener-attached, not read-only portfolio addresses.

    30s in-process cache because the /arb Balances tab polls every 10s
    and balances don't move outside of order events. We explicitly
    invalidate the cache on order placed / closed via
    invalidate_positions_cache, so user-visible freshness stays sub-second
    after their own actions."""
    import time as _time
    cached = _BALANCES_CACHE.get(user_id)
    if cached and (_time.time() - cached[0]) < _BALANCES_CACHE_TTL_S:
        return cached[1]
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
    out = [r for r in results if isinstance(r, dict)]
    _BALANCES_CACHE[user_id] = (_time.time(), out)
    return out


# ── Spot / short pair detection ──────────────────────────────────────────────
# A user holding 0.5 BTC spot on Binance + a short BTC perp on Bybit is
# implicitly running a spot/short basis trade. Surface those pairs in /arb
# the same way long/short pairs work, so funding accrual and PnL are
# reported as one position rather than two unrelated rows.
#
# Data sources:
#   - Spot side: BalanceSnapshot.totals — already populated by the
#     portfolio fetcher every cycle. Asset symbol + qty per wallet, no
#     extra API calls needed.
#   - Short side: list_user_positions() — futures positions across all
#     trade-enabled wallets.
#
# Pair matching rules (mirrors _pnl_can_pair for long/short):
#   1. Same symbol on both legs.
#   2. Spot notional within ±5% of short notional (approximate basis-
#      neutral hedge — overshooting the spot side is a stronger basis bet
#      and worth surfacing as a maybe-pair, undershooting too).
#   3. If the spot side's snapshot_at is available, opened within 10 min
#      of the short. Otherwise (snapshot lag/no time), we surface the
#      pair with `time_match: null` so the user can confirm.
#
# Pair decisions live in the same TradePairDecision table — leg_a_key
# becomes "<symbol>|spot|<wallet_id>" and leg_b_key "<symbol>|<short_ex>|<wallet_id>".
_SPOT_STABLE = {"USDT", "USDC", "USD", "DAI", "FDUSD", "TUSD", "BUSD"}
_SPOT_NOTIONAL_TOLERANCE_PCT = 5.0
_SPOT_TIME_WINDOW_S = 10 * 60


def _list_user_spot_holdings(db: Session, user_id: int) -> list[dict]:
    """Per-(wallet, asset) spot holdings from BalanceSnapshot.totals.

    Returns rows shaped:
      {wallet_id, exchange, asset, qty, snapshot_at}
    Stablecoins filtered out (they're the quote side, not a tradable
    asset for spot/short matching).
    """
    from backend.db.models import BalanceSnapshot
    rows = (
        db.query(BalanceSnapshot, Wallet)
        .join(Wallet, Wallet.id == BalanceSnapshot.wallet_id)
        .filter(
            BalanceSnapshot.user_id == user_id,
            Wallet.is_archived == False,  # noqa: E712
            Wallet.wallet_type.in_(("exchange", "perpdex")),
        )
        .all()
    )
    out: list[dict] = []
    for snap, wallet in rows:
        totals = snap.totals or {}
        if not isinstance(totals, dict):
            continue
        for asset, raw_qty in totals.items():
            asset_u = (asset or "").upper()
            if not asset_u or asset_u in _SPOT_STABLE:
                continue
            try:
                qty = float(raw_qty) if not isinstance(raw_qty, dict) else float(raw_qty.get("total") or 0)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            out.append({
                "wallet_id": wallet.id,
                "exchange": wallet.type_value,
                "wallet_name": wallet.name,
                "asset": asset_u,
                "qty": qty,
                "snapshot_at": snap.snapshot_at.isoformat() if snap.snapshot_at else None,
            })
    return out


def _spot_short_pair_decision_keys(symbol: str, spot_wallet_id: int,
                                     short_ex: str, short_wallet_id: int) -> tuple[str, str]:
    """Decision keys used when persisting a manual spot/short pair-decision.

    Same column shape as long/short pairs so we don't need to touch the
    schema, just convention: leg_a is always the spot side.
    """
    return (f"{symbol}|spot|{spot_wallet_id}",
            f"{symbol}|{short_ex}|{short_wallet_id}")


def _spot_short_can_pair(spot: dict, short: dict, spot_price: float | None) -> tuple[bool, str | None]:
    """Notional + (best-effort) time match. Returns (ok, reason)."""
    if not spot_price or spot_price <= 0:
        return False, "no spot price"
    short_qty = float(short.get("quantity") or 0)
    short_entry = float(short.get("entry_price") or 0)
    spot_qty = float(spot.get("qty") or 0)
    if short_qty <= 0 or short_entry <= 0 or spot_qty <= 0:
        return False, "zero qty/price"
    short_notional = short_qty * short_entry
    spot_notional = spot_qty * spot_price
    base = max(short_notional, spot_notional)
    if base <= 0:
        return False, "zero notional"
    diff_pct = abs(short_notional - spot_notional) / base * 100.0
    if diff_pct > _SPOT_NOTIONAL_TOLERANCE_PCT:
        return False, f"notional diff {diff_pct:.1f}% > {_SPOT_NOTIONAL_TOLERANCE_PCT:.0f}%"
    return True, f"notional within {diff_pct:.1f}%"


async def _spot_price_lookup(symbols: list[str]) -> dict[str, float]:
    """Pull last-trade price for each base from the screener's funding cache —
    avoids per-symbol REST calls. Falls back to 0 (caller treats as unknown)."""
    out: dict[str, float] = {}
    try:
        from backend.services.arbitrage_service import _cache as _arb_cache
    except Exception:
        return out
    for ex_name, (rows, _ts) in _arb_cache.items():
        for r in rows or []:
            sym = (r.get("symbol") or "").upper()
            px = r.get("price")
            if not sym or not isinstance(px, (int, float)) or sym in out:
                continue
            if sym in symbols:
                out[sym] = float(px)
        if len(out) == len(symbols):
            break
    return out


async def list_user_spot_short_pairs(db: Session, user_id: int) -> list[dict]:
    """For each open SHORT position, surface matching SPOT holdings as pair
    candidates. The frontend renders these alongside long/short pairs
    on the /arb pair card so basis traders see one row per real position.
    """
    positions = await list_user_positions(db, user_id)
    shorts = [p for p in positions if (p.get("side") or "").lower() == "sell"]
    if not shorts:
        return []
    spots = _list_user_spot_holdings(db, user_id)
    if not spots:
        return []

    # Index spot holdings by asset for O(1) lookup
    spot_by_asset: dict[str, list[dict]] = {}
    for s in spots:
        spot_by_asset.setdefault(s["asset"], []).append(s)

    # Read pair-decision table once — the same table powers long/short and
    # spot/short, distinguished by leg_a_key prefix "spot|".
    from backend.db.models import TradePairDecision
    decisions = {
        (d.leg_a_key, d.leg_b_key): d.decision
        for d in db.query(TradePairDecision).filter(TradePairDecision.user_id == user_id).all()
    }

    # One last_trade_price lookup for every short symbol — avoids 1-call-per-pair.
    sym_set = sorted({(p.get("symbol") or "").upper() for p in shorts})
    px_map = await _spot_price_lookup(sym_set)

    out: list[dict] = []
    for short in shorts:
        sym = (short.get("symbol") or "").upper()
        if sym not in spot_by_asset:
            continue
        spot_price = px_map.get(sym) or float(short.get("entry_price") or 0)
        for spot in spot_by_asset[sym]:
            ok, reason = _spot_short_can_pair(spot, short, spot_price)
            if not ok:
                continue
            leg_a, leg_b = _spot_short_pair_decision_keys(
                sym, spot["wallet_id"],
                (short.get("exchange") or "").lower(), short.get("wallet_id") or 0,
            )
            decision = decisions.get((leg_a, leg_b))
            if decision == "unpaired":
                continue  # user explicitly rejected this pair
            out.append({
                "kind": "spot_short_pair",
                "symbol": sym,
                "spot": {
                    "wallet_id": spot["wallet_id"],
                    "exchange": spot["exchange"],
                    "wallet_name": spot["wallet_name"],
                    "qty": spot["qty"],
                    "qty_usd": spot["qty"] * spot_price,
                    "snapshot_at": spot["snapshot_at"],
                },
                "short": short,
                "auto_paired": (decision == "paired") or (decision is None),
                "decision": decision or "auto",
                "match_reason": reason,
                "spot_price_estimate": spot_price,
            })
    return out
