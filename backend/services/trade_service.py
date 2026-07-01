"""Unified trade dispatcher — resolves user's wallet for a given exchange and
delegates to the per-exchange adapter.
"""
from __future__ import annotations

import asyncio
import logging
import os as _os
from typing import Any

from datetime import datetime

from sqlalchemy.orm import Session

from backend.crypto import decrypt_credentials
from backend.db.models import Wallet, TradeOrder
from backend.services.trade_adapters import ADAPTERS, SUPPORTED_EXCHANGES

logger = logging.getLogger("avalant.trade")


# ── Python-fallback safety guard (close path) ────────────────────────────
# Some Python adapters implement close_position with a semantics that does
# NOT match the requested market_type. If we silently fall back from Go to
# Python here, we can close the WRONG leg of an arb pair.
#
# Known traps:
#   backpack — Python close_position sells the full base-asset SPOT balance
#              (backpack.py:245). The Go adapter has proper perp close via
#              /api/v1/position. If a user has a perp position open and Go
#              fails (network blip, etc.), the Python fallback would dump
#              their spot balance — wrong leg on a spot/short arb pair.
#
# Add a venue to this set ONLY after auditing that adapter's
# close_position to confirm the same-market trap. The dispatcher refuses
# fallback and surfaces a clear error instead of routing through Python.
_PYTHON_CLOSE_FALLBACK_UNSAFE: dict[tuple[str, str], str] = {
    ("backpack", "futures"): (
        "Backpack Python close_position is a spot-sell — would close the "
        "wrong leg of a perp/spot arb. Go close path required. Retry once Go "
        "engine is reachable; do not close manually via spot in the meantime."
    ),
}


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
            Wallet.wallet_type.in_(("exchange", "perpdex")),
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
            Wallet.wallet_type.in_(("exchange", "perpdex")),
            Wallet.type_value == exchange.lower(),
            Wallet.is_archived == False,  # noqa: E712
        )
        .order_by(Wallet.id.desc())
        .first()
    )


# Module-level monotonic-clock helper for cache TTL maths. get_pair_status
# (and _get_live_mark) reference _mono(); previously it was only imported
# locally inside _list_user_positions_inner, so /api/trade/status raised
# NameError on every call → /arb hits the endpoint, console spammed with
# 500s. Use the same impl arbitrage_service ships so cache_at_mono values
# inter-op if ever shared.
import time as _time_mod
def _mono() -> float:
    return _time_mod.monotonic()


_PAIR_STATUS_CACHE: dict[tuple, tuple[dict, float]] = {}
# 3s TTL: /arb trade panel polls /trade/status каждые 5s now (was 15s),
# и юзер должен видеть свежий balance после external moves (deposit/
# withdraw напрямую на venue без нашей платформы). 15s было слишком
# консервативно — balance lag в trade panel ощутим.
_PAIR_STATUS_TTL = 3.0


def invalidate_pair_status_cache(user_id: int, exchange: str | None = None) -> None:
    """Drop cached pair-status for a user. Called after place_order / close
    so the next /trade/status call shows fresh balance. If exchange is
    given, only entries touching that exchange are dropped."""
    if exchange is None:
        # Drop all entries for this user.
        to_del = [k for k in _PAIR_STATUS_CACHE if k[0] == user_id]
    else:
        to_del = [k for k in _PAIR_STATUS_CACHE if k[0] == user_id and exchange in (k[2], k[3])]
    for k in to_del:
        _PAIR_STATUS_CACHE.pop(k, None)


async def get_pair_status(db: Session, user_id: int, symbol: str, long_ex: str, short_ex: str) -> dict:
    """Per-leg trading readiness for an arb pair.
    Returns: { long: {wallet_id, status, balance_usdt}, short: {...} }

    Cached 15s per (user_id, symbol, long_ex, short_ex). Без кеша делает
    2 live HTTP-вызова к venue API (fetch_balance × 2) которые суммарно
    дают 200-3000ms задержки. Invalidated в place_open_order/close после
    каждого ордера так что balance отражает свежее состояние.
    """
    cache_key = (user_id, symbol, long_ex, short_ex)
    now_m = _mono()
    cached = _PAIR_STATUS_CACHE.get(cache_key)
    if cached and (now_m - cached[1]) < _PAIR_STATUS_TTL:
        return cached[0]

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
        balance_error = None
        if status == "ok" and w is not None:
            try:
                creds = decrypt_credentials(w.credentials or {})
                bal = await ADAPTERS[ex].fetch_balance(creds)
                balance = round(float(bal.get("usdt", 0) or 0), 2)
                logger.info("balance %s wallet=%s usdt=%.2f raw=%s",
                            ex, w.id, balance, bal)
            except Exception as exc:
                emsg = str(exc)
                logger.warning("Balance fetch FAILED for %s wallet=%s uid=%s: %s: %s",
                               ex, w.id, user_id, type(exc).__name__, emsg)
                # Surface a short, actionable hint to the UI. Detect
                # common venue-side rejection patterns so user sees what
                # to do instead of generic "0 USDT".
                if "10010" in emsg or "Unmatched IP" in emsg or "IP" in emsg and "whitelist" in emsg.lower():
                    balance_error = "IP not whitelisted on the API key — add prod IP on the venue."
                elif "10003" in emsg or "10004" in emsg or "Invalid" in emsg.lower() and "key" in emsg.lower():
                    balance_error = "API key invalid or revoked — re-issue on the venue."
                elif "permission" in emsg.lower() or "forbidden" in emsg.lower():
                    balance_error = "API key lacks required permission (Read + Trade Futures)."
                else:
                    balance_error = emsg[:140]
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
            "balance_error": balance_error,
            "exchange": ex,
        }

    # Subtract pending-open-trigger reservations so the LT panel can
    # show effective available capital (balance − reservations) and the
    # % allocation slider sizes against capital the user can actually
    # commit. Reservations are computed once for the user and applied to
    # both legs by wallet_id.
    try:
        reservations = _pending_open_trigger_reservations(db, user_id)
        for leg in ("long", "short"):
            wid = out[leg].get("wallet_id")
            bal = out[leg].get("balance_usdt")
            reserved = float(reservations.get(wid, 0.0)) if wid else 0.0
            out[leg]["reserved_usdt"] = round(reserved, 2)
            if bal is not None:
                out[leg]["available_usdt"] = round(max(0.0, float(bal) - reserved), 2)
            else:
                out[leg]["available_usdt"] = None
    except Exception:
        # Don't break the trade panel if reservation calc fails — fall
        # back to showing balance as available.
        for leg in ("long", "short"):
            bal = out[leg].get("balance_usdt")
            out[leg].setdefault("reserved_usdt", 0.0)
            out[leg].setdefault("available_usdt", bal)
    # Cache + return — capped at ~1000 entries (purged in invalidate when
    # the user trades). For a typical web worker with hundreds of users
    # this is well under MB-scale memory.
    _PAIR_STATUS_CACHE[cache_key] = (out, now_m)
    return out


async def place_open_order(
    db: Session, user_id: int,
    wallet_id: int, symbol: str, side: str, quantity: float,
    leverage: int, margin_mode: str,
    *, market_type: str = "futures",
    order_type: str = "market",
    limit_price: float | None = None,
    stop_price: float | None = None,
) -> dict:
    # Normalise inputs
    symbol = (symbol or "").strip().upper()
    if not symbol or not symbol.isalnum() or len(symbol) > 16:
        raise TradeError(f"Invalid symbol: {symbol!r}", kind="user")
    if side not in ("buy", "sell"):
        raise TradeError(f"Invalid side: {side!r}", kind="user")
    if market_type not in ("futures", "spot"):
        raise TradeError(f"Invalid market_type: {market_type!r}", kind="user")
    is_spot = market_type == "spot"
    # Spot is always 1× / cash — leverage + margin_mode irrelevant.
    if not is_spot:
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

    # Inject cached balance from user-stream snapshot — adapter's
    # preflight can use it as a hint to skip the REST `fetch_balance`
    # round-trip (~50-100ms saved per order on LIVE-stream venues).
    # Adapter ignores the hint if it doesn't know about it.
    try:
        from backend.services.user_streams import _snapshot as _us_snapshot
        if _us_snapshot.get_status(user_id, wallet_id) == "LIVE":
            cached_bal = _us_snapshot.get_balance(user_id, wallet_id)
            if cached_bal is not None:
                creds["_cached_balance_usdt"] = float(cached_bal)
    except Exception:
        pass

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

    # Log the pending row BEFORE preflight + signing — gives us a
    # permanent audit trail even if preflight rejects (min-qty too
    # small, balance insufficient, etc). Without this the user only
    # sees the leg that made it past preflight in Order History; the
    # other leg's failure is silently invisible.
    order_row = _log_order(
        db, user_id=user_id, wallet_id=wallet_id, exchange=ex, symbol=symbol,
        side=side, intent="open", requested_qty=quantity, leverage=leverage,
        margin_mode=margin_mode, status="pending",
    )

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
                reason = pre.get("reason") or "Pre-flight check failed"
                _finalize_order(db, order_row, status="failed",
                                error_kind="user", error_message=reason)
                raise TradeError(reason, kind="user")
            if pre.get("qty_rounded"):
                quantity = float(pre["qty_rounded"])
                # Update the row with the rounded qty so Order History
                # reflects what we actually attempted.
                order_row.requested_qty = quantity
                db.commit()
        except TradeError:
            raise
        except Exception as exc:
            logger.info("preflight unexpected error %s/%s: %s", ex, symbol, exc)

    await leverage_task

    try:
        # Go-engine fast path: when the venue is on the cutover list and
        # the proxy is reachable, dispatch over to go-fetcher for the
        # signing + roundtrip. Any failure (network blip, missing env,
        # unsupported venue) falls back to the local Python adapter so
        # a proxy outage never blocks a user's order.
        result = None
        from backend.services import trade_proxy
        if trade_proxy.is_enabled(ex):
            try:
                result = await trade_proxy.place_order(
                    ex, creds, symbol, side, quantity,
                    leverage=leverage, margin_mode=margin_mode,
                    market_type=market_type,
                    order_type=order_type,
                    limit_price=limit_price,
                    stop_price=stop_price,
                )
                logger.info("Order placed via go-fetcher: ex=%s sym=%s market=%s order_id=%s",
                            ex, symbol, market_type, result.get("order_id"))
            except trade_proxy.GoTradeError as gerr:
                if gerr.kind in ("user", "exchange"):
                    # Same outcome we'd get from the local adapter —
                    # surface to the user, no fallback (the order genuinely
                    # cannot succeed regardless of which engine signs it).
                    _state_cache.invalidate(ex, creds, symbol)
                    _finalize_order(db, order_row, status="failed",
                                    error_kind=gerr.kind, error_message=gerr.message)
                    raise TradeError(gerr.message, kind=gerr.kind)
                logger.warning("Go proxy failed (%s) — falling back to Python: %s",
                               gerr.kind, gerr.message)
                result = None
        if result is None:
            if is_spot:
                # Python adapters don't have a spot path yet — refuse with
                # a clear message rather than silently routing through the
                # futures method.
                raise TradeError(
                    f"Spot trading on {ex} requires the Go proxy "
                    f"(GO_TRADE_VENUES) and a SpotAdapter implementation.",
                    kind="user",
                )
            # Python adapters only implement market orders. If the user
            # requested a limit/stop/tp order and we've fallen through
            # to the Python path (Go proxy down or errored), REFUSE
            # rather than silently opening a market at current price.
            # Prior behaviour: adapter.place_order was called without
            # order_type/limit_price/stop_price → base signature
            # accepted the args and shipped a market. Users had no way
            # to know their limit order became an immediate market.
            if order_type != "market":
                raise TradeError(
                    f"{order_type} orders temporarily unavailable on {ex} "
                    f"(Go proxy required — this venue is currently on the "
                    f"Python fallback). Use a market order or retry in a "
                    f"few seconds.",
                    kind="user",
                )
            result = await adapter.place_order(creds, symbol, side, quantity,
                                               leverage=leverage, margin_mode=margin_mode)
    except TradeError:
        raise
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
    # Balance changed — drop any cached /trade/status for this user/venue
    # so the next status call reflects the fill.
    try:
        invalidate_pair_status_cache(user_id, ex)
    except Exception:
        pass
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
    *, market_type: str = "futures",
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
        # Same Go-engine fast path as place_open_order. Falls back to
        # the local adapter on any transient/internal failure.
        result = None
        from backend.services import trade_proxy
        if trade_proxy.is_enabled(ex):
            try:
                result = await trade_proxy.close_position(ex, creds, symbol, side or "",
                                                          market_type=market_type)
                logger.info("Position closed via go-fetcher: ex=%s sym=%s market=%s",
                            ex, symbol, market_type)
            except trade_proxy.GoTradeError as gerr:
                if gerr.kind in ("user", "exchange"):
                    _finalize_order(db, order_row, status="failed",
                                    error_kind=gerr.kind, error_message=gerr.message)
                    raise TradeError(gerr.message, kind=gerr.kind)
                logger.warning("Go proxy close failed (%s) — falling back: %s",
                               gerr.kind, gerr.message)
                result = None
        if result is None:
            # Safety guard: refuse Python fallback for venues whose Python
            # close_position has a different market semantic than requested
            # (see _PYTHON_CLOSE_FALLBACK_UNSAFE at the top of this module).
            # Otherwise we'd close the WRONG leg of an arb pair.
            unsafe_msg = _PYTHON_CLOSE_FALLBACK_UNSAFE.get((ex, market_type))
            if unsafe_msg:
                _finalize_order(db, order_row, status="failed",
                                error_kind="user", error_message=unsafe_msg)
                logger.error("Python close fallback REFUSED for %s %s: %s",
                             ex, market_type, unsafe_msg)
                raise TradeError(unsafe_msg, kind="user")
            result = await ADAPTERS[ex].close_position(creds, symbol, side or "")
    except TradeError:
        raise
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
    try:
        invalidate_pair_status_cache(user_id, ex)
    except Exception:
        pass
    # Phase 4 — schedule a background fills_backfill sync so the realized
    # PNL on this position becomes accurate within ~5-15s instead of the
    # hardcoded 0.0 most Python close adapters return. Non-blocking: the
    # close response goes out immediately; the backfill polls userTrades /
    # income for this wallet and emits a corrected trade_positions row
    # over the top of the close's provisional one.
    try:
        from backend.services import fills_backfill_service
        async def _bg_backfill():
            try:
                await fills_backfill_service.sync_user(user_id)
            except Exception as exc:
                logger.warning("post-close fills_backfill sync failed: %s", exc)
        asyncio.create_task(_bg_backfill())
    except Exception as exc:
        logger.debug("post-close fills_backfill schedule failed: %s", exc)
    return result


# ── Local UPNL (Phase 1.1) ──────────────────────────────────────────────
# When AVALANT_LOCAL_UPNL=1 we override the venue-supplied mark_price + UPNL
# with our local recompute. Source for mark: arbitrage_service._cache (live
# funding/ticker rows from screener feed, refreshed ~250ms-2s by go-fetcher
# funding.json + REST backstops). Formula: (mark − entry) × qty × side_dir.
#
# Rationale: venue's unrealized_pnl_usd is a snapshot from the last
# position-event push or REST list_positions call. On a quiet pair between
# events, mark on screen lags actual market — sometimes for minutes. Reading
# mark from our own screener-tick feed gives a sub-second freshness boundary.
#
# Behind a flag so we can verify against venue's UPNL on a live position
# before flipping. STATUS: code-ready, awaits Phase 1 acceptance on a real
# position (creds gate).
_LOCAL_UPNL_ENABLED = (_os.getenv("AVALANT_LOCAL_UPNL") or "0").strip() == "1"
_LOCAL_UPNL_MAX_MARK_AGE_S = float(_os.getenv("AVALANT_LOCAL_UPNL_MAX_AGE_S", "10.0"))
# Phase 1.2 — pair leg sync. Pair = stale when |leg_a_age − leg_b_age| > this.
# Frontend uses pair_mark_stale(leg_a, leg_b) to decide whether to grey out the
# sum-PNL diff. Default 2s: typical arbitrage_service _cache rebuild is
# ~250ms-2s per venue, so >2s skew almost always means one feed lagging.
_PAIR_MARK_MAX_DIFF_S = float(_os.getenv("AVALANT_PAIR_MARK_MAX_DIFF_S", "2.0"))


def _get_live_mark(exchange: str, symbol: str) -> tuple[float | None, float | None, float | None]:
    """Look up live mark price for (exchange, symbol) in arbitrage_service._cache.

    Returns (mark_price, age_seconds, cached_at_mono) or (None, ...) on miss.
    The third tuple element (cached_at_mono) is the monotonic timestamp of
    the underlying cache snapshot — Phase 1.2 uses it to detect rasync
    between two legs of a pair.
    Symbol matched case-insensitively against rows[i]['symbol'].
    """
    try:
        from backend.services.arbitrage_service import _cache, _mono
    except Exception:
        return None, None, None
    bucket = _cache.get((exchange or "").lower())
    if not bucket:
        return None, None, None
    rows, cached_at_mono = bucket
    if not rows:
        return None, None, None
    age_s = _mono() - cached_at_mono
    if age_s > _LOCAL_UPNL_MAX_MARK_AGE_S:
        return None, age_s, cached_at_mono
    sym_norm = (symbol or "").upper()
    for r in rows:
        rs = (r.get("symbol") or "").upper()
        if rs == sym_norm:
            p = r.get("price")
            if p is None:
                return None, age_s, cached_at_mono
            try:
                return float(p), age_s, cached_at_mono
            except (TypeError, ValueError):
                return None, age_s, cached_at_mono
    return None, age_s, cached_at_mono


def pair_mark_stale(leg_a: dict, leg_b: dict) -> bool:
    """Phase 1.2 — return True when the two legs' marks come from snapshots
    more than _PAIR_MARK_MAX_DIFF_S apart. Used by frontend (and Phase 2
    server-side pair grouping) to mark the diff-PNL as `stale` rather than
    showing a phantom delta from feed rasync.

    Both legs need `mark_tick_ts` (added by _apply_local_upnl when flag on).
    Without it (flag off / no live mark) returns False — fall back to whatever
    the venue surfaced.
    """
    a_ts = leg_a.get("mark_tick_ts")
    b_ts = leg_b.get("mark_tick_ts")
    if a_ts is None or b_ts is None:
        return False
    try:
        return abs(float(a_ts) - float(b_ts)) > _PAIR_MARK_MAX_DIFF_S
    except (TypeError, ValueError):
        return False


def _apply_local_upnl(rows: list[dict]) -> None:
    """In-place override of mark + unrealized_pnl_usd per position when
    AVALANT_LOCAL_UPNL=1 AND we have a fresh live mark. Adds `mark_source`
    and `mark_age_s` for frontend visibility.

    No-op when flag off, or row has no entry_price, or no fresh live mark.
    """
    if not _LOCAL_UPNL_ENABLED:
        return
    for r in rows:
        ex = (r.get("exchange") or "").lower()
        sym = r.get("symbol") or ""
        entry = r.get("entry_price")
        qty = r.get("quantity")
        side = (r.get("side") or "").lower()
        try:
            entry_f = float(entry or 0)
            qty_f = float(qty or 0)
        except (TypeError, ValueError):
            r["mark_source"] = "venue"
            continue
        if entry_f <= 0 or qty_f <= 0 or side not in ("buy", "sell"):
            r["mark_source"] = "venue"
            continue
        live_mark, age_s, tick_ts = _get_live_mark(ex, sym)
        if live_mark is None or live_mark <= 0:
            r["mark_source"] = "venue"
            if age_s is not None:
                r["mark_age_s"] = round(age_s, 2)
            continue
        side_dir = 1.0 if side == "buy" else -1.0
        r["mark_price"] = live_mark
        r["unrealized_pnl_usd"] = round(
            (live_mark - entry_f) * qty_f * side_dir, 6
        )
        r["mark_source"] = "live"
        r["mark_age_s"] = round(age_s, 2)
        # Phase 1.2 — absolute mono-ts of the cache snapshot. Frontend (or
        # Phase 2 grouping) uses pair_mark_stale(leg_a, leg_b) to detect
        # |a_ts − b_ts| > threshold and mark the pair diff stale.
        if tick_ts is not None:
            r["mark_tick_ts"] = round(tick_ts, 4)


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
# 30s was too tight given exchanges occasionally take 6-10s under load
# (gate, kucoin) — successive timeouts within a 30s window dropped the
# row from the UI even though the position was still open. 5 minutes
# easily covers transient API slowness; we still drop on a confirmed
# successful empty response (legitimately-closed positions).
_POSITIONS_LASTGOOD_TTL_S = 300.0


async def list_user_positions(db: Session, user_id: int, symbol: str | None = None,
                              *, authoritative: bool = False) -> list[dict]:
    """Aggregate open positions across all the user's trade-enabled wallets.

    Two latency optimisations (UI path):
      - Stale-while-revalidate: if we have ANY cache (even past TTL),
        return it immediately and kick off a background refresh. Cold
        page load still has to wait, but refreshes never block the UI.
      - Per-wallet timeout: each exchange's list_positions is capped at
        _POSITIONS_PER_WALLET_TIMEOUT_S. A misbehaving venue (MEXC with
        an IP-whitelist 60s hang, etc.) doesn't drag the whole call out
        — that wallet's last-good (if any) covers the gap.

    authoritative=True (reconcile path): skip cache + lastgood, return
    only fresh per-wallet data. Venues that timeout return [] for that
    wallet rather than resurrecting stale rows. Prevents phantom-open
    positions from sticking when an exchange's REST is unreachable.
    """
    if authoritative:
        return await _list_user_positions_inner(db, user_id, symbol,
                                                 fresh_only=True)
    import time as _time
    cache_key = (user_id, (symbol or "").upper())
    cached = _POSITIONS_CACHE.get(cache_key)
    now = _time.time()
    if cached:
        age = now - cached[0]
        if age < _POSITIONS_CACHE_TTL_S:
            return cached[1]
        if age < _POSITIONS_STALE_MAX_S and not _POSITIONS_REFRESH_INFLIGHT.get(cache_key):
            _POSITIONS_REFRESH_INFLIGHT[cache_key] = True
            async def _bg():
                try:
                    from backend.db.base import SessionLocal
                    bg_db = SessionLocal()
                    try:
                        await _list_user_positions_inner(bg_db, user_id, symbol)
                    finally:
                        bg_db.close()
                finally:
                    _POSITIONS_REFRESH_INFLIGHT.pop(cache_key, None)
            asyncio.create_task(_bg())
            return cached[1]
    return await _list_user_positions_inner(db, user_id, symbol)


_POSITIONS_STALE_MAX_S = 30 * 60.0  # extended from 5m to 30m: "came back after >5min" returns stale+bg-refresh
_POSITIONS_REFRESH_INFLIGHT: dict[tuple[int, str], bool] = {}

# Prometheus counter: positions REST fallback events per exchange.
# Incremented whenever a wallet falls through to REST because WS stream is
# not LIVE. Exposed via /api/metrics for operational alerting.
_POSITIONS_REST_FALLBACK_COUNT: dict[str, int] = {}
# Per-wallet REST timeout. Env override: AVALANT_POSITIONS_TIMEOUT_S (float, default 5.0).
# Reduced from 10s: a misbehaving exchange times out in 5s and serves lastgood; the
# parallel gather caps total latency at max(per_wallet_times), so this directly bounds
# the worst-case response time when WS user-stream is dead.
import os as _os
_POSITIONS_PER_WALLET_TIMEOUT_S = float(_os.getenv("AVALANT_POSITIONS_TIMEOUT_S", "5.0"))


async def _list_user_positions_inner(db: Session, user_id: int, symbol: str | None,
                                      *, fresh_only: bool = False) -> list[dict]:
    import time as _time
    cache_key = (user_id, (symbol or "").upper())
    wallets = (
        db.query(Wallet)
        .filter(
            Wallet.user_id == user_id,
            Wallet.wallet_type.in_(("exchange", "perpdex")),
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

        # WS user-stream short-circuit (UI path only). For reconcile
        # (fresh_only=True) we always hit REST so a stale WS snapshot
        # doesn't resurrect a closed position.
        if not fresh_only:
            try:
                from backend.services.user_streams import _snapshot as _us_snapshot
                ws_status = _us_snapshot.get_status(user_id, w.id)
                if ws_status == "LIVE":
                    rows = _us_snapshot.get_positions(user_id, w.id) or []
                    if symbol:
                        sym_norm = symbol.upper().strip()
                        rows = [r for r in rows if (r.get("symbol") or "").upper() == sym_norm]
                    if rows:
                        for r in rows:
                            r["wallet_id"] = w.id
                        _POSITIONS_LASTGOOD[lg_key] = (_time.time(), rows)
                        return rows
                    # Empty snapshot — fall through to REST.
                else:
                    # WS not LIVE → positions will come from REST (6-9s path).
                    _POSITIONS_REST_FALLBACK_COUNT[w.type_value] = (
                        _POSITIONS_REST_FALLBACK_COUNT.get(w.type_value, 0) + 1
                    )
                    logger.info(
                        "positions REST fallback: wallet=%s ex=%s ws_status=%s "
                        "(user_id=%s); timeout=%.0fs",
                        w.id, w.type_value, ws_status, user_id,
                        _POSITIONS_PER_WALLET_TIMEOUT_S,
                    )
            except Exception as exc:
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
            symbol_not_on_venue = any(s in msg for s in ("51001", "-1121", "Instrument ID", "Invalid symbol"))
            if symbol_not_on_venue:
                logger.debug("list_positions skipped wallet=%s ex=%s: %s", w.id, w.type_value, msg)
                return []
            logger.info("list_positions failed wallet=%s ex=%s: %s", w.id, w.type_value, exc)
            # Authoritative path: do NOT serve lastgood — reconcile must
            # not keep a phantom position open just because the venue is
            # unreachable. Empty result lets reconcile decide what to do
            # (after N misses, force-close).
            if fresh_only:
                return []
            lg = _POSITIONS_LASTGOOD.get(lg_key)
            if lg and (_time.time() - lg[0]) < _POSITIONS_LASTGOOD_TTL_S:
                return lg[1]
            return []

    async def _one_capped(w: Wallet) -> list[dict]:
        # Per-wallet hard timeout. A slow exchange (MEXC w/ IP-whitelist
        # hang, etc.) gets dropped from this snapshot.
        try:
            return await asyncio.wait_for(
                _one(w), timeout=_POSITIONS_PER_WALLET_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            lg_key = (user_id, w.id, (symbol or "").upper())
            # Authoritative path bypasses lastgood entirely.
            if fresh_only:
                logger.info("list_positions wallet=%s ex=%s timed out (%.1fs) — fresh_only=[]",
                            w.id, w.type_value, _POSITIONS_PER_WALLET_TIMEOUT_S)
                return []
            lg = _POSITIONS_LASTGOOD.get(lg_key)
            if lg and (_time.time() - lg[0]) < _POSITIONS_LASTGOOD_TTL_S:
                return lg[1]
            logger.info("list_positions wallet=%s ex=%s timed out (%.1fs)",
                        w.id, w.type_value, _POSITIONS_PER_WALLET_TIMEOUT_S)
            return []

    results = await asyncio.gather(
        *(_one_capped(w) for w in wallets), return_exceptions=True
    )
    flat: list[dict] = []
    for r in results:
        if isinstance(r, list):
            flat.extend(r)
    # Phase 1.1 — override mark + UPNL from live screener feed when flag is
    # on. No-op when AVALANT_LOCAL_UPNL=0 (default). Done BEFORE caching so
    # the override propagates through the cache TTL the same way it would
    # propagate live.
    _apply_local_upnl(flat)
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


def set_spot_short_pair_decision(db: Session, user_id: int, symbol: str,
                                   spot_wallet_id: int,
                                   short_exchange: str, short_wallet_id: int,
                                   decision: str) -> None:
    """Persist a spot/short pair decision. Uses the same TradePairDecision
    table — leg_a is always the spot side, conventionally keyed
    "<symbol>|spot|<wallet_id>"."""
    if decision not in ("paired", "unpaired"):
        raise TradeError("Invalid decision", kind="user")
    leg_a, leg_b = _spot_short_pair_decision_keys(
        (symbol or "").upper(), int(spot_wallet_id),
        (short_exchange or "").lower(), int(short_wallet_id),
    )
    from backend.db.models import TradePairDecision
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
        db.add(TradePairDecision(
            user_id=user_id, leg_a_key=leg_a, leg_b_key=leg_b, decision=decision,
        ))
    db.commit()
    logger.info(
        "Spot/short pair decision: user=%s sym=%s spot_wallet=%s short_ex=%s short_wallet=%s decision=%s",
        user_id, symbol, spot_wallet_id, short_exchange, short_wallet_id, decision,
    )


def list_pair_decisions(db: Session, user_id: int, live_positions: list[dict] | None = None) -> list[dict]:
    """Return active (decision != 'unpaired') pair decisions for the user.

    The frontend uses this to pre-populate the manual-pair list, replacing
    the legacy localStorage cache. Unpaired decisions stay in the DB so we
    can avoid re-suggesting the same auto-pair the user has already
    rejected, but we don't surface them to the UI.

    A 'paired' row stays in the DB after the underlying positions close —
    that's intentional (the same decision applies if the pair re-opens
    later). But the Sync ⇆ UI should only list pairs that are actually
    LIVE; otherwise the user has to "unpair" something that's already
    closed just to clear the dialog. We cross-reference each decision
    against `live_positions` and only return pairs where at least one
    leg is still open.
    """
    from backend.db.models import TradePairDecision
    rows = (
        db.query(TradePairDecision)
        .filter(TradePairDecision.user_id == user_id,
                TradePairDecision.decision == "paired")
        .all()
    )
    open_keys: set[tuple[str, str, str]] = set()
    if live_positions is not None:
        for p in live_positions:
            sym = (p.get("symbol") or "").upper()
            ex = (p.get("exchange") or "").lower()
            side = (p.get("side") or "").lower()
            if not sym or not ex or not side:
                continue
            if abs(float(p.get("quantity") or 0)) <= 0:
                continue
            open_keys.add((sym, ex, side))
    out: list[dict] = []
    for r in rows:
        # leg_a_key = "SYM|long_ex|buy", leg_b_key = "SYM|short_ex|sell"
        # Skip spot/short pair decisions — those use leg_a "spot|<wallet_id>"
        # and are surfaced by list_user_spot_short_pairs, not here.
        if r.leg_a_key.startswith("spot|") or "|spot|" in r.leg_a_key:
            continue
        try:
            sym, long_ex, _ = r.leg_a_key.split("|")
            _, short_ex, _ = r.leg_b_key.split("|")
        except ValueError:
            continue
        if live_positions is not None:
            sym_u = sym.upper()
            long_open = (sym_u, long_ex.lower(), "buy") in open_keys
            short_open = (sym_u, short_ex.lower(), "sell") in open_keys
            if not long_open and not short_open:
                continue  # pair is fully closed → don't surface in Sync UI
        out.append({"symbol": sym, "long_exchange": long_ex, "short_exchange": short_ex})
    return out


# ── Live arb-pair grouping (Phase 2) ────────────────────────────────────
# Server-side equivalent of frontend's _acc_pair_positions. Same 12%
# tolerance + 5-min window + manual-first ordering, but importantly also
# evaluates pair_mark_stale() so the pair is tagged with `mark_stale=True`
# when the two legs' live marks come from desynced screener snapshots.
#
# Manual decisions are persisted in TradePairDecision (table
# trade_pair_decisions) — same source frontend's _loadManualPairs reads
# from. So a user's Sync ⇆ click on /arb survives a page reload because
# it routed through /api/trade/pair/sync (set_pair_decision) which writes
# the DB row.
_PAIR_NOTIONAL_TOLERANCE_PCT = float(_os.getenv("AVALANT_PAIR_NOTIONAL_TOL_PCT", "12.0"))


def _pair_key(symbol: str, exchange: str, side: str) -> tuple[str, str, str]:
    return (symbol.upper(), exchange.lower(), side.lower())


def _load_manual_pairs_for_user(db: Session, user_id: int) -> list[dict]:
    """Returns 'paired' long_short decisions only (spot/short uses its own
    table-side handler). Each entry: {symbol, long_exchange, short_exchange}."""
    from backend.db.models import TradePairDecision
    rows = (
        db.query(TradePairDecision)
        .filter(TradePairDecision.user_id == user_id,
                TradePairDecision.decision == "paired")
        .all()
    )
    out: list[dict] = []
    for r in rows:
        if r.leg_a_key.startswith("spot|") or "|spot|" in r.leg_a_key:
            continue
        try:
            sym, long_ex, _ = r.leg_a_key.split("|")
            _, short_ex, _ = r.leg_b_key.split("|")
        except ValueError:
            continue
        out.append({
            "symbol": sym.upper(),
            "long_exchange": long_ex.lower(),
            "short_exchange": short_ex.lower(),
        })
    return out


def group_live_positions(
    positions: list[dict],
    manual_pairs: list[dict] | None = None,
) -> dict:
    """Group flat list of live positions into {pairs, singles}.

    Same logic as frontend _acc_pair_positions (arb.js:4312):
      1. Manual pairs first — symbol+long_ex+short_ex matched exactly.
      2. Auto-detect remaining — same symbol, opposite sides, notional diff%
         within spread% ± _PAIR_NOTIONAL_TOLERANCE_PCT (default 12%).
    Best-candidate first per symbol = smallest |diff% − spread%|.

    Each emitted pair also gets `mark_stale=True` if pair_mark_stale()
    (Phase 1.2) detects rasync between the two legs' live marks.

    Returns {
        'pairs':  [{symbol, long: <position-dict>, short: <position-dict>,
                    _manual: bool, mark_stale: bool}],
        'singles': [<position-dict>, ...],
    }
    """
    manual = manual_pairs or []
    tagged = []
    for i, p in enumerate(positions):
        try:
            qty = abs(float(p.get("quantity") or 0))
            mark = float(p.get("mark_price") or 0)
        except (TypeError, ValueError):
            qty, mark = 0.0, 0.0
        key = p.get("position_id") or f"{(p.get('exchange') or '').lower()}:{(p.get('symbol') or '').upper()}:{i}"
        tagged.append({"p": p, "key": key, "notional": qty * mark})

    by_sym: dict[str, list[dict]] = {}
    for t in tagged:
        sym = (t["p"].get("symbol") or "").upper()
        by_sym.setdefault(sym, []).append(t)

    pairs: list[dict] = []
    used: set = set()

    # 1. Manual pairs first — exact match on (symbol, long_ex, short_ex).
    for mp in manual:
        sym = mp["symbol"].upper()
        long_ex = mp["long_exchange"].lower()
        short_ex = mp["short_exchange"].lower()
        group = by_sym.get(sym)
        if not group:
            continue
        l = next((t for t in group
                  if (t["p"].get("exchange") or "").lower() == long_ex
                  and (t["p"].get("side") or "").lower() == "buy"
                  and t["key"] not in used), None)
        s = next((t for t in group
                  if (t["p"].get("exchange") or "").lower() == short_ex
                  and (t["p"].get("side") or "").lower() == "sell"
                  and t["key"] not in used), None)
        if not l or not s:
            continue
        used.add(l["key"]); used.add(s["key"])
        pairs.append({
            "symbol": sym,
            "long": l["p"], "short": s["p"],
            "_manual": True,
            "mark_stale": pair_mark_stale(l["p"], s["p"]),
        })

    # 2. Auto-detect for the rest.
    for sym, group in by_sym.items():
        longs = [t for t in group
                 if (t["p"].get("side") or "").lower() == "buy" and t["key"] not in used]
        shorts = [t for t in group
                  if (t["p"].get("side") or "").lower() == "sell" and t["key"] not in used]
        candidates = []
        for l in longs:
            for s in shorts:
                max_n = max(l["notional"], s["notional"])
                if max_n <= 0:
                    continue
                try:
                    le = float(l["p"].get("entry_price") or 0)
                    se = float(s["p"].get("entry_price") or 0)
                except (TypeError, ValueError):
                    continue
                spread_pct = (abs((se - le) / le) * 100.0) if le > 0 and se > 0 else 0.0
                diff_pct = abs(l["notional"] - s["notional"]) / max_n * 100.0
                if abs(diff_pct - spread_pct) > _PAIR_NOTIONAL_TOLERANCE_PCT:
                    continue
                candidates.append({"l": l, "s": s,
                                   "deviation": abs(diff_pct - spread_pct)})
        # Best (smallest deviation) first.
        candidates.sort(key=lambda c: c["deviation"])
        for c in candidates:
            if c["l"]["key"] in used or c["s"]["key"] in used:
                continue
            used.add(c["l"]["key"]); used.add(c["s"]["key"])
            pairs.append({
                "symbol": sym,
                "long": c["l"]["p"], "short": c["s"]["p"],
                "_manual": False,
                "mark_stale": pair_mark_stale(c["l"]["p"], c["s"]["p"]),
            })

    singles = [t["p"] for t in tagged if t["key"] not in used]
    return {"pairs": pairs, "singles": singles}


def list_user_pnl_pending_pairs(db: Session, user_id: int, *, days: int = 30) -> list[dict]:
    """Phase 4 — return arb pairs in 'partially_closed' state for the PNL
    tab's Pending section.

    A pair is 'partially_closed' when ONE leg has status='closed' and the
    counterpart still has status='open'. The realized leg's PNL is real,
    the open leg's PNL is unrealized → the diff isn't a valid arb result
    yet. The PNL view filters these out of the realized list (correct);
    this function surfaces them so the UI shows 'pair pending close: X
    of 2 legs'.

    Same paring rule as list_user_pnl (_pnl_can_pair with 12% tolerance +
    5-min window, plus manual paired decisions).
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

    open_by_sym_side: dict[tuple[str, str], list] = {}
    for r in open_rows:
        key = ((r.symbol or "").upper(), (r.leg_a_side or "").lower())
        open_by_sym_side.setdefault(key, []).append(r)

    pending: list[dict] = []
    seen: set[int] = set()
    for c in closed:
        if c.id in seen:
            continue
        sym = (c.symbol or "").upper()
        side = (c.leg_a_side or "").lower()
        opp_side = "sell" if side == "buy" else "buy"
        opp_opens = open_by_sym_side.get((sym, opp_side), [])
        if not opp_opens:
            continue
        for op in opp_opens:
            # Same _pnl_can_pair rule used everywhere else.
            l_pos, s_pos = (c, op) if side == "buy" else (op, c)
            if not _pnl_can_pair(l_pos, s_pos, paired, unpaired):
                continue
            seen.add(c.id)
            pending.append({
                "symbol": sym,
                "status": "partially_closed",
                "legs_closed": 1,
                "legs_total": 2,
                "closed_leg": {
                    "exchange": (c.leg_a_exchange or "").lower(),
                    "side": side,
                    "realized_pnl_usd": float(c.realized_pnl_usd or 0),
                    "closed_at": c.closed_at.isoformat() if c.closed_at else None,
                },
                "open_leg": {
                    "exchange": (op.leg_a_exchange or "").lower(),
                    "side": opp_side,
                    "opened_at": op.opened_at.isoformat() if op.opened_at else None,
                },
            })
            break
    return pending


async def list_user_arb_pairs(db: Session, user_id: int,
                              symbol: str | None = None) -> dict:
    """Public API: fetch live positions + group into pairs server-side.

    Replaces the frontend-only grouping. Frontend can call this endpoint
    instead of doing the 12%-tolerance matching itself, which (a) reuses
    the canonical rule (one source of truth, no JS-vs-Python drift) and
    (b) lets the result feed alerts/triggers/audit that the browser
    can't reach.
    """
    positions = await list_user_positions(db, user_id, symbol)
    manual = _load_manual_pairs_for_user(db, user_id)
    return group_live_positions(positions, manual_pairs=manual)


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
    # 12% tolerance (was 5%) — see _acc_pair_positions in arb.html for
    # the matching rationale. Real-world arbs scale legs unevenly when
    # entering at different times, so 5% missed obvious pairs.
    if abs(diff_pct - spread_pct) > 12.0:
        return False
    # 5-minute opening window
    if long_pos.opened_at and short_pos.opened_at:
        delta = abs((long_pos.opened_at - short_pos.opened_at).total_seconds())
        if delta > 5 * 60:
            return False
    return True


def _spot_short_paired_keys(db: Session, user_id: int) -> set[tuple[str, str]]:
    """Return {(symbol, short_exchange)} for which the user has an active
    spot/short pair-decision marked 'paired'. Used to tag closed shorts
    in the P&L tab so the UI hints that the spot side's PnL lives on the
    venue rather than in our DB."""
    from backend.db.models import TradePairDecision
    out: set[tuple[str, str]] = set()
    rows = (
        db.query(TradePairDecision)
        .filter(
            TradePairDecision.user_id == user_id,
            TradePairDecision.decision == "paired",
        )
        .all()
    )
    for r in rows:
        if not (r.leg_a_key or "").startswith(""):
            continue
        try:
            sym, leg_a_kind, _ = (r.leg_a_key or "").split("|")
        except ValueError:
            continue
        if leg_a_kind != "spot":
            continue
        try:
            _, short_ex, _ = (r.leg_b_key or "").split("|")
        except ValueError:
            continue
        out.add((sym.upper(), short_ex.lower()))
    return out


def _pnl_can_pair_spot_short(spot_long, futures_short, paired: set, unpaired: set) -> bool:
    """Auto-pair rule for closed spot LONG + closed futures SHORT.

    A basis-trade closure is two events: (1) selling the spot holding,
    materialized as a closed kind='single' leg_a_market='spot' side='buy';
    and (2) buying back the futures short, materialized as a closed
    side='sell' leg_a_market='futures'. We pair them if:
      - same symbol
      - notional within 12% (same tolerance as long/short auto-pair)
      - closed within 5 min of each other
    User overrides via TradePairDecision still apply. Decision keys reuse
    the spot|<sym>|<wallet_id> ↔ <sym>|<short_ex>|<short_wallet_id>
    pattern so the live spot/short flow and the historical one share
    storage.
    """
    sym = (spot_long.symbol or "").upper()
    if (futures_short.symbol or "").upper() != sym:
        return False
    spot_wallet = spot_long.leg_a_wallet_id or 0
    short_ex = (futures_short.leg_a_exchange or "").lower()
    short_wallet = futures_short.leg_a_wallet_id or 0
    leg_a_key = f"{sym}|spot|{spot_wallet}"
    leg_b_key = f"{sym}|{short_ex}|{short_wallet}"
    if (leg_a_key, leg_b_key) in unpaired:
        return False
    if (leg_a_key, leg_b_key) in paired:
        return True
    le = float(spot_long.leg_a_entry_price or 0)
    se = float(futures_short.leg_a_entry_price or 0)
    if le <= 0 or se <= 0:
        return False
    long_n = float(spot_long.leg_a_qty or 0) * le
    short_n = float(futures_short.leg_a_qty or 0) * se
    max_n = max(long_n, short_n)
    if max_n <= 0:
        return False
    diff_pct = abs(long_n - short_n) / max_n * 100.0
    if diff_pct > 12.0:
        return False
    if spot_long.closed_at and futures_short.closed_at:
        delta = abs((spot_long.closed_at - futures_short.closed_at).total_seconds())
        if delta > 5 * 60:
            return False
    return True


def _spot_short_decisions_keyed(db: Session, user_id: int) -> tuple[set, set]:
    """Return (paired, unpaired) sets keyed by (leg_a_key, leg_b_key) for
    spot/short pair decisions — leg_a starts with `<sym>|spot|...`."""
    from backend.db.models import TradePairDecision
    paired: set = set()
    unpaired: set = set()
    rows = db.query(TradePairDecision).filter(TradePairDecision.user_id == user_id).all()
    for r in rows:
        a = r.leg_a_key or ""
        b = r.leg_b_key or ""
        if "|spot|" not in a:
            continue
        key = (a, b)
        (paired if r.decision == "paired" else unpaired).add(key)
    return paired, unpaired


def list_user_pnl(db: Session, user_id: int, *, days: int = 30) -> list[dict]:
    """P&L tab — closed positions over the last `days` days, grouped into
    pairs where pair-decision OR auto-detect (spread%±5% / 5-min window)
    applies. Partial-closed pairs (one leg closed, the other still open)
    are filtered out — those still belong in the live Positions tab.

    Two pairing passes run on the closed singles:
      1. Long/Short on futures legs (existing rule).
      2. Spot LONG + Futures SHORT (basis trade — new in fills-backfill).
    Closed SHORT positions whose user has a 'paired' spot/short decision
    on the same (symbol, exchange) are tagged `paired_with_spot=True` so
    the UI can show them as basis-trade closures instead of plain shorts.
    """
    from backend.db.models import TradePosition
    from datetime import timedelta as _td
    cutoff = datetime.utcnow() - _td(days=int(days))
    paired, unpaired = _pnl_pair_decisions(db, user_id)
    spot_paired_keys = _spot_short_paired_keys(db, user_id)
    spot_paired_set, spot_unpaired_set = _spot_short_decisions_keyed(db, user_id)

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

    # Pre-pass: spot LONG + futures SHORT auto-pair (basis trade closures).
    # Runs first because a closed spot row has the same shape as a normal
    # single — without this pass it would render as a stand-alone "spot"
    # row and the user would need to mentally pair them up.
    spot_longs = [c for c in closed
                  if c.id not in used_ids
                  and (c.leg_a_market or "futures") == "spot"
                  and (c.leg_a_side or "").lower() == "buy"]
    fut_shorts = [c for c in closed
                  if c.id not in used_ids
                  and (c.leg_a_market or "futures") == "futures"
                  and (c.leg_a_side or "").lower() == "sell"]
    for sl in spot_longs:
        if sl.id in used_ids:
            continue
        for fs in fut_shorts:
            if fs.id in used_ids:
                continue
            if _pnl_can_pair_spot_short(sl, fs, spot_paired_set, spot_unpaired_set):
                used_ids.add(sl.id)
                used_ids.add(fs.id)
                out.append(_serialize_pnl_spot_short(sl, fs))
                break

    # First pass: pair up closed singles into pair rows.
    for r in closed:
        if r.id in used_ids:
            continue
        # Skip spot rows — they only pair via the spot/short pass above; if
        # they got here unmatched they render as stand-alone spot singles.
        if (r.leg_a_market or "futures") != "futures":
            sym = (r.symbol or "").upper()
            side = (r.leg_a_side or "").lower()
            used_ids.add(r.id)
            out.append(_serialize_pnl_single(r))
            continue
        sym = (r.symbol or "").upper()
        side = (r.leg_a_side or "").lower()
        opp_side = "sell" if side == "buy" else "buy"
        # Restrict counterparts to FUTURES legs — spot can't pair via the
        # long/short rule.
        candidates = [c for c in by_sym_side.get((sym, opp_side), [])
                      if c.id not in used_ids
                      and (c.leg_a_market or "futures") == "futures"]

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
        # Tag closed shorts that had a paired spot leg — the spot PnL lives
        # on the venue, not in our DB, so we surface it as a basis-trade
        # closure rather than a plain single short.
        is_short = side == "sell"
        ex_lc = (r.leg_a_exchange or "").lower()
        paired_w_spot = is_short and (sym, ex_lc) in spot_paired_keys
        out.append(_serialize_pnl_single(r, paired_with_spot=paired_w_spot))

    return out


def _serialize_pnl_single(r, *, paired_with_spot: bool = False) -> dict:
    return {
        "kind": "spot_short_paired" if paired_with_spot else "single",
        "id": r.id,
        "symbol": r.symbol,
        "exchange": r.leg_a_exchange,
        "market": r.leg_a_market or "futures",
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
        "source": r.source or "platform",
        # Tag set on shorts that had a paired spot leg via TradePairDecision.
        # The spot leg's PnL isn't tracked here (we don't keep historical
        # balance snapshots), so the row is marked so the UI can hint the
        # full P&L lives on the venue's spot history.
        "paired_with_spot": paired_with_spot,
    }


def _serialize_pnl_spot_short(spot_long, futures_short) -> dict:
    """Serialize a closed spot/short basis pair. Mirrors the long/short
    serializer shape so the frontend can render either kind with one
    component, just keyed off `pair_kind`."""
    le = float(spot_long.leg_a_entry_price or 0)
    se = float(futures_short.leg_a_entry_price or 0)
    long_realized = spot_long.leg_a_realized_pnl_usd or 0
    short_realized = futures_short.leg_a_realized_pnl_usd or 0
    long_funding = spot_long.leg_a_funding_pnl_usd or 0
    short_funding = futures_short.leg_a_funding_pnl_usd or 0
    long_fees = spot_long.leg_a_fees_usd or 0
    short_fees = futures_short.leg_a_fees_usd or 0
    total = long_realized + short_realized + long_funding + short_funding - long_fees - short_fees
    spread_pct = abs((se - le) / le) * 100.0 if le > 0 else None
    opened_at = max(filter(None, [spot_long.opened_at, futures_short.opened_at])) \
        if (spot_long.opened_at or futures_short.opened_at) else None
    closed_at = max(filter(None, [spot_long.closed_at, futures_short.closed_at])) \
        if (spot_long.closed_at or futures_short.closed_at) else None
    return {
        "kind": "pair",
        "pair_kind": "spot_short",
        "id": f"{spot_long.id}-{futures_short.id}",
        "symbol": spot_long.symbol,
        "long":  {
            "exchange": spot_long.leg_a_exchange,
            "market": "spot",
            "qty": spot_long.leg_a_qty,
            "entry_price": spot_long.leg_a_entry_price,
            "exit_price": spot_long.leg_a_exit_price,
            "realized_pnl_usd": long_realized,
            "funding_pnl_usd": long_funding,
            "fees_usd": long_fees,
            "opened_externally": bool(spot_long.opened_externally),
            "closed_externally": bool(spot_long.closed_externally),
        },
        "short": {
            "exchange": futures_short.leg_a_exchange,
            "market": "futures",
            "qty": futures_short.leg_a_qty,
            "entry_price": futures_short.leg_a_entry_price,
            "exit_price": futures_short.leg_a_exit_price,
            "realized_pnl_usd": short_realized,
            "funding_pnl_usd": short_funding,
            "fees_usd": short_fees,
            "opened_externally": bool(futures_short.opened_externally),
            "closed_externally": bool(futures_short.closed_externally),
        },
        "total_realized_pnl_usd": long_realized + short_realized,
        "total_funding_pnl_usd": long_funding + short_funding,
        "total_fees_usd": long_fees + short_fees,
        "total_pnl_usd": total,
        "entry_spread_pct": spread_pct,
        "opened_at": opened_at.isoformat() if opened_at else None,
        "closed_at": closed_at.isoformat() if closed_at else None,
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
            Wallet.wallet_type.in_(("exchange", "perpdex")),
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
            "spot_usdt":       None,
            "futures_usdt":    None,
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
        # Trade adapters return {"usdt": float, "spot_usd": float,
        # "futures_usd": float} (flat). Older adapter shapes may return
        # just {"usdt": float} or wallet-provider style {"USDT": {free,total}}.
        # The `or`-chain trick is unsafe here — a literal 0.0 is falsy and
        # would skip a real-but-zero balance — so we test each key with
        # explicit `is not None`.
        usdt = None
        spot_v = None
        fut_v = None
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
            if usdt is None:
                v = bal.get("available")
                if v is None: v = bal.get("free")
                if v is None: v = bal.get("equity")
                try: usdt = float(v) if v is not None else None
                except (TypeError, ValueError): usdt = None
            try:
                if bal.get("spot_usd") is not None:
                    spot_v = float(bal["spot_usd"])
            except (TypeError, ValueError): pass
            try:
                if bal.get("futures_usd") is not None:
                    fut_v = float(bal["futures_usd"])
            except (TypeError, ValueError): pass
        try:
            out["balance_usdt"] = round(float(usdt), 2) if usdt is not None else None
        except (TypeError, ValueError):
            out["balance_usdt"] = None
        out["spot_usdt"]    = round(spot_v, 2) if spot_v is not None else None
        out["futures_usdt"] = round(fut_v,  2) if fut_v  is not None else None
        return out

    results = await asyncio.gather(*(_one(w) for w in wallets), return_exceptions=True)
    out = [r for r in results if isinstance(r, dict)]
    # Subtract reservations from active open-triggers so the user sees
    # "available" = balance - already-committed-by-pending-orders.
    # Only kind='open' triggers reserve fresh capital; close/tp/sl reduce
    # an existing position so they don't lock new margin.
    reservations = _pending_open_trigger_reservations(db, user_id)
    for row in out:
        wid = row.get("wallet_id")
        bal = row.get("balance_usdt")
        reserved = float(reservations.get(wid, 0.0))
        row["reserved_usdt"]  = round(reserved, 2)
        if bal is not None:
            row["available_usdt"] = round(max(0.0, float(bal) - reserved), 2)
        else:
            row["available_usdt"] = None
    _BALANCES_CACHE[user_id] = (_time.time(), out)
    return out


def _pending_open_trigger_reservations(db: Session, user_id: int) -> dict[int, float]:
    """USD reserved per wallet by active 'open' triggers — pending,
    firing, or scheduled. Subtracted from balance_usdt so the user sees
    real available capital and can't oversize when chaining triggers
    across symbols.

    Reservation per leg:
      long_short pair: notional / leverage on EACH wallet
      spot_short pair: full notional on the spot leg (1x), notional /
                      leverage on the short (perp) leg

    Mark price comes from the prices cache (CMC/Gate-aggregated, ≤30s
    fresh) — exact-cent accuracy isn't critical, ballpark is enough to
    prevent reckless oversizing.
    """
    from backend.db.models import ArbTriggerOrder
    rows = (
        db.query(ArbTriggerOrder)
        .filter(
            ArbTriggerOrder.user_id == user_id,
            ArbTriggerOrder.kind == "open",
            ArbTriggerOrder.status.in_(("pending", "firing", "scheduled")),
        )
        .all()
    )
    if not rows:
        return {}

    # Resolve mark prices once. price_service caches the CMC top-100,
    # so it covers BTC/ETH/SOL but not most alts (SPACEX, VANRY, etc).
    # For anything missing, fall back to the live orderbook mid-price
    # from books.json — which the go-fetcher dumps every 100ms for any
    # symbol on any subscribed venue.
    try:
        from backend.services import price_service
        all_prices = dict(price_service.price_cache_snapshot() or {})
    except Exception:
        all_prices = {}

    # Lazy-load books.json for orderbook fallback — we only read once
    # per call regardless of how many triggers we're processing.
    _books = None
    def _book_mid(ex: str, sym: str) -> float:
        nonlocal _books
        if _books is None:
            try:
                import json, os
                cache_dir = os.environ.get("AVALANT_FETCHER_CACHE_DIR", "/tmp/avalant_cache")
                with open(os.path.join(cache_dir, "books.json")) as f:
                    _books = json.load(f)
            except Exception:
                _books = {}
        entry = _books.get(f"{ex.lower()}:{sym.upper()}") if isinstance(_books, dict) else None
        if not isinstance(entry, dict):
            return 0.0
        bids = entry.get("bids") or []
        asks = entry.get("asks") or []
        try:
            top_bid = float(bids[0][0]) if bids else 0.0
            top_ask = float(asks[0][0]) if asks else 0.0
            if top_bid > 0 and top_ask > 0:
                return (top_bid + top_ask) / 2.0
            return top_bid or top_ask
        except (TypeError, ValueError, IndexError):
            return 0.0

    res: dict[int, float] = {}
    for r in rows:
        target = r.portions_target or 1
        filled = r.portions_filled or 0
        if r.infinite_fill:
            # Infinite-fill triggers refill one chunk at a time. Reserve
            # exactly one chunk's worth — anything more would lock all
            # the user's capital indefinitely.
            qty_remaining = r.portion_size_token or r.total_qty_token or 0
        elif r.portion_size_token:
            qty_remaining = r.portion_size_token * max(0, target - filled)
        else:
            qty_remaining = (r.total_qty_token or 0) if filled < target else 0
        if qty_remaining <= 0:
            continue

        sym = (r.long_symbol or "").upper()
        mark = float(all_prices.get(sym) or 0)
        # Fallback for non-CMC alts: use live orderbook mid from
        # books.json. Try long leg first (the side we always quote
        # against), short as backup.
        if mark <= 0 and r.long_exchange:
            mark = _book_mid(r.long_exchange, sym)
        if mark <= 0 and r.short_exchange:
            mark = _book_mid(r.short_exchange, sym)
        if mark <= 0:
            continue
        notional_usd = qty_remaining * mark
        leverage = max(1, int(r.leverage or 1))

        # Long leg
        if r.long_wallet_id:
            # In long_short, both legs use leverage. In spot_short, the
            # long is spot — full notional, no leverage discount.
            long_lev = 1 if _is_spot_short(r) else leverage
            res[r.long_wallet_id] = res.get(r.long_wallet_id, 0.0) + notional_usd / long_lev
        # Short leg always perp → leveraged
        if r.short_wallet_id:
            res[r.short_wallet_id] = res.get(r.short_wallet_id, 0.0) + notional_usd / leverage

    return res


def _is_spot_short(r) -> bool:
    """Best-effort: a trigger row's pair_kind isn't directly stored, but
    arb_position (if linked) carries it. Conservative default = long_short
    (uses leverage on both legs)."""
    try:
        if r.arb_position is not None and (r.arb_position.kind or "") == "spot_short":
            return True
    except Exception:
        pass
    return False


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
_SPOT_NOTIONAL_TOLERANCE_PCT = 12.0
_SPOT_TIME_WINDOW_S = 10 * 60

# How stale a BalanceSnapshot can be before /spot-short-pairs forces a
# refresh. Most venues take 1-3s for a balance fetch; 5 min keeps the
# endpoint cheap on warm caches but fresh enough for new spot purchases
# to land in the spot/short pair card without a manual /app refresh.
_SPOT_REFRESH_STALE_S = 5 * 60
# Hard timeout for the on-demand refresh — any single venue that takes
# longer than this is dropped. Refresh runs in the background, the
# request itself never waits on it.
_SPOT_REFRESH_TIMEOUT_S = 30.0
# Per-user dedup so multiple /arb tabs / fast polls don't queue
# overlapping background refreshes against the same exchange APIs.
_SPOT_REFRESH_INFLIGHT: dict[int, bool] = {}


async def _refresh_stale_spot_snapshots(db: Session, user_id: int, shorts: list[dict]) -> None:
    """Trigger a fresh balance fetch for spot-capable wallets whose snapshot
    is older than _SPOT_REFRESH_STALE_S, when the user has open SHORT
    positions that could plausibly pair with spot.

    Why only when shorts exist: refreshing all spot venues on every page
    load would be wasteful. Spot/short pair detection only fires when
    there are open shorts; if there are none, stale snapshots don't
    matter for THIS endpoint.

    We refresh ALL spot-capable wallets (not just the ones holding the
    short's asset) because the user might have moved spot to a different
    venue since the last snapshot and we'd never see the new venue's
    holding otherwise.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from backend.db.models import BalanceSnapshot
    from backend.services import balance_service

    if not shorts:
        return

    cutoff = _dt.now(_tz.utc) - _td(seconds=_SPOT_REFRESH_STALE_S)
    wallets = (
        db.query(Wallet)
        .filter(
            Wallet.user_id == user_id,
            Wallet.is_archived == False,  # noqa: E712
            Wallet.wallet_type.in_(("exchange", "perpdex")),
        )
        .all()
    )
    if not wallets:
        return

    snap_age: dict[int, _dt | None] = {}
    rows = (
        db.query(BalanceSnapshot.wallet_id, BalanceSnapshot.snapshot_at)
        .filter(BalanceSnapshot.user_id == user_id)
        .all()
    )
    for wid, ts in rows:
        snap_age[int(wid)] = ts

    stale: list[Wallet] = []
    for w in wallets:
        ts = snap_age.get(int(w.id))
        if ts is None:
            stale.append(w)
            continue
        # Naive datetimes from SQLite — assume UTC for comparison.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz.utc)
        if ts < cutoff:
            stale.append(w)

    if not stale:
        return

    # Fire-and-forget: kick off the refresh in the background and
    # return immediately. Blocking the request for up to 12s while
    # balances stream in slowed down /arb's first paint by a lot —
    # the user reported "open pair page very slow when I have a
    # position there". Next /spot-short-pairs poll (frontend hits
    # it every ~10s) will see the fresh data once it lands.
    #
    # Per-user dedup so simultaneous /arb tabs don't queue overlapping
    # refreshes for the same wallets.
    if _SPOT_REFRESH_INFLIGHT.get(user_id):
        return
    _SPOT_REFRESH_INFLIGHT[user_id] = True

    async def _bg():
        # Build a fresh DB session — the request's session may close
        # before this finishes.
        from backend.db.base import SessionLocal
        bg_db = SessionLocal()
        try:
            await asyncio.wait_for(
                balance_service.fetch_balances(stale, bg_db),
                timeout=_SPOT_REFRESH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.info(
                "spot-short refresh timed out after %.0fs (user=%s, wallets=%d)",
                _SPOT_REFRESH_TIMEOUT_S, user_id, len(stale),
            )
        except Exception as exc:
            logger.warning("spot-short refresh failed user=%s: %s", user_id, exc)
        finally:
            bg_db.close()
            _SPOT_REFRESH_INFLIGHT.pop(user_id, None)

    asyncio.create_task(_bg())


async def _spot_avg_entries(
    db: Session,
    user_id: int,
    spot_by_asset: dict[str, list[dict]],
    shorts: list[dict],
) -> dict[tuple[int, str], float | None]:
    """For every (wallet, asset) pair where the asset matches an open
    short, fetch the spot cost basis from the venue's trade history.
    Returns {(wallet_id, asset): avg_entry_price | None}.

    Per-venue: each provider exposes a `spot_avg_entry(creds, sym, qty)`
    method. We skip exchanges that don't implement it (their pairs fall
    back to the paired-open assumption on the frontend).

    Cached 5 min in-process so repeated /arb polls don't re-walk trade
    history every 10s — these calls cost real rate-limit budget.
    """
    if not shorts:
        return {}
    short_assets = {(p.get("symbol") or "").upper() for p in shorts}
    if not short_assets:
        return {}

    from backend.providers.exchanges import EXCHANGE_PROVIDERS
    import time as _time
    out: dict[tuple[int, str], float | None] = {}
    tasks: list[tuple[tuple[int, str], Any]] = []

    for asset, holdings in spot_by_asset.items():
        if asset not in short_assets:
            continue
        for h in holdings:
            wid = int(h["wallet_id"])
            cache_key = (wid, asset)
            cached = _SPOT_ENTRY_CACHE.get(cache_key)
            if cached and (_time.time() - cached[0]) < _SPOT_ENTRY_TTL_S:
                out[cache_key] = cached[1]
                continue
            wallet = db.query(Wallet).get(wid)
            if not wallet:
                continue
            provider_cls = EXCHANGE_PROVIDERS.get(wallet.type_value)
            if not provider_cls:
                continue
            provider = provider_cls()
            fn = getattr(provider, "spot_avg_entry", None)
            if not fn:
                continue
            try:
                creds_dict = decrypt_credentials(wallet.credentials or {})
                creds = {
                    "api_key": creds_dict.get("api_key", ""),
                    "api_secret": creds_dict.get("api_secret", ""),
                    "api_passphrase": creds_dict.get("api_passphrase", ""),
                }
                tasks.append((cache_key, fn(creds, asset, float(h["qty"]))))
            except Exception:
                continue

    if tasks:
        results = await asyncio.gather(*(t[1] for t in tasks), return_exceptions=True)
        now = _time.time()
        for (cache_key, _coro), r in zip(tasks, results):
            val = r if isinstance(r, (int, float)) and r > 0 else None
            _SPOT_ENTRY_CACHE[cache_key] = (now, val)
            out[cache_key] = val
    return out


_SPOT_ENTRY_CACHE: dict[tuple[int, str], tuple[float, float | None]] = {}
_SPOT_ENTRY_TTL_S = 5 * 60.0


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


def _spot_short_can_pair(spot: dict, short, spot_price: float | None) -> tuple[bool, str | None]:
    """Notional + best-effort time match. Returns (ok, reason).

    Notional match uses CURRENT marks for both legs — short_qty × short_mark
    vs spot_qty × spot_price — so a price drift since open doesn't make
    the function reject a real pair. The earlier code compared
    short_qty × short_ENTRY against spot_qty × spot_MARK, which on any
    pair held through a meaningful price move blew past the 5% tolerance
    even though the dollar exposure on both legs was still balanced.

    Time match: if both `spot.snapshot_at` and `short.opened_at` are
    available, require them within ±10 min. The spot snapshot_at is the
    last balance refresh, NOT the actual purchase time. Older snapshots
    just mean the spot pre-existed the short — we still surface the
    pair as a candidate, just flagged in the reason string.
    """
    if not spot_price or spot_price <= 0:
        return False, "no spot price"
    is_dict = isinstance(short, dict)
    short_qty = float((short.get("quantity") if is_dict else getattr(short, "quantity", 0)) or 0)
    short_entry = float((short.get("entry_price") if is_dict else getattr(short, "entry_price", 0)) or 0)
    short_mark = float((short.get("mark_price") if is_dict else getattr(short, "mark_price", 0)) or 0)
    spot_qty = float(spot.get("qty") or 0)
    if short_qty <= 0 or spot_qty <= 0:
        return False, "zero qty"
    # Use the current mark when available — matches how spot is valued
    # (live price). Falls back to entry if mark is missing.
    short_notional_now = short_qty * (short_mark if short_mark > 0 else short_entry)
    spot_notional_now = spot_qty * spot_price
    base = max(short_notional_now, spot_notional_now)
    if base <= 0:
        return False, "zero notional"
    diff_pct = abs(short_notional_now - spot_notional_now) / base * 100.0
    if diff_pct > _SPOT_NOTIONAL_TOLERANCE_PCT:
        return False, f"notional diff {diff_pct:.1f}% > {_SPOT_NOTIONAL_TOLERANCE_PCT:.0f}%"

    # Time match — both timestamps optional.
    short_opened = short.get("opened_at") if isinstance(short, dict) else getattr(short, "opened_at", None)
    spot_snap = spot.get("snapshot_at")
    time_match: bool | None = None
    if short_opened and spot_snap:
        try:
            from datetime import datetime as _dt
            so = short_opened if hasattr(short_opened, "year") else _dt.fromisoformat(str(short_opened).replace("Z", "+00:00"))
            ss = _dt.fromisoformat(str(spot_snap).replace("Z", "+00:00"))
            if so.tzinfo and not ss.tzinfo:
                ss = ss.replace(tzinfo=so.tzinfo)
            elif ss.tzinfo and not so.tzinfo:
                so = so.replace(tzinfo=ss.tzinfo)
            delta = abs((ss - so).total_seconds())
            time_match = delta <= _SPOT_TIME_WINDOW_S
        except Exception:
            time_match = None

    if time_match is True:
        return True, f"notional within {diff_pct:.1f}% + time match (≤10 min)"
    if time_match is False:
        # Notional matches but spot snapshot far from short open — surface
        # as a manual-confirm pair with a hint that the time window didn't
        # match. Frontend can color-code this differently.
        return True, f"notional within {diff_pct:.1f}% (spot held outside ±10 min window)"
    return True, f"notional within {diff_pct:.1f}%"


async def _spot_price_lookup(symbols: list[str]) -> dict[str, float]:
    """Pull last-trade price for each base from go-fetcher's funding.json.

    After the Go-fetcher cutover, arbitrage_service._cache (Python) is no
    longer populated — prices live in /tmp/avalant_cache/funding.json
    written by go-fetcher's funding dumper. We pick the first non-zero
    price per symbol. Spot ≈ perp price for the basis math we do here.
    Falls back to {} (caller treats missing keys as unknown and uses
    short.entry_price as a last resort).
    """
    out: dict[str, float] = {}
    if not symbols:
        return out
    want = {s.upper() for s in symbols if s}
    try:
        import json as _json
        import os as _os
        cache_dir = _os.environ.get("AVALANT_CACHE_DIR", "/tmp/avalant_cache")
        path = _os.path.join(cache_dir, "funding.json")
        with open(path, "r") as f:
            doc = _json.load(f)
    except Exception:
        return out
    for r in doc.get("rows") or []:
        sym = (r.get("symbol") or "").upper()
        if sym not in want or sym in out:
            continue
        px = r.get("price")
        if isinstance(px, (int, float)) and px > 0:
            out[sym] = float(px)
        if len(out) == len(want):
            break
    return out


async def list_user_spot_short_pairs(db: Session, user_id: int) -> list[dict]:
    """For each open SHORT position, surface matching SPOT holdings as pair
    candidates. The frontend renders these alongside long/short pairs
    on the /arb pair card so basis traders see one row per real position.

    Live SHORT data (qty, entry_price) comes from list_user_positions; the
    open-time stamp is enriched from TradePosition rows so the time-window
    match in _spot_short_can_pair has something to compare against.

    Spot freshness: if any spot-capable wallet's BalanceSnapshot is older
    than _SPOT_REFRESH_STALE_S we kick off a fresh balance fetch for those
    wallets in parallel before computing pairs. Without this the user has
    to manually click 'Refresh' on /app every time they want spot/short
    pairs to reflect a fresh spot purchase. We bound the refresh to a
    short timeout so the endpoint stays responsive even if some venue
    is slow.
    """
    from backend.db.models import TradePosition
    positions = await list_user_positions(db, user_id)
    shorts = [p for p in positions if (p.get("side") or "").lower() == "sell"]
    if not shorts:
        return []
    await _refresh_stale_spot_snapshots(db, user_id, shorts)
    spots = _list_user_spot_holdings(db, user_id)
    if not spots:
        return []

    # Enrich live shorts with opened_at from the persistent table.
    db_opens = (
        db.query(TradePosition)
        .filter(
            TradePosition.user_id == user_id,
            TradePosition.kind == "single",
            TradePosition.status == "open",
            TradePosition.leg_a_side == "sell",
        )
        .all()
    )
    opened_at_by_key: dict[tuple[str, int], Any] = {}
    for r in db_opens:
        sym = (r.symbol or "").upper()
        if r.leg_a_wallet_id:
            opened_at_by_key[(sym, int(r.leg_a_wallet_id))] = r.opened_at
    for s in shorts:
        sym = (s.get("symbol") or "").upper()
        wid = s.get("wallet_id")
        if sym and wid is not None:
            s.setdefault("opened_at", opened_at_by_key.get((sym, int(wid))))

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

    # Real spot cost-basis per (wallet, asset) — falls back to None when
    # the exchange has no avg-entry helper or the API is unauthorized.
    avg_entries = await _spot_avg_entries(db, user_id, spot_by_asset, shorts)

    out: list[dict] = []
    for short in shorts:
        sym = (short.get("symbol") or "").upper()
        if sym not in spot_by_asset:
            continue
        spot_price = px_map.get(sym) or float(short.get("entry_price") or 0)
        for spot in spot_by_asset[sym]:
            # _spot_short_can_pair now returns (ok, reason) where ok=False
            # means notional/time mismatch but the ticker still matches.
            # Per user spec: surface ALL ticker matches in the API
            # response — the Sync UI uses this for manual decisions.
            # auto_paired flips to True only when the strict check passes
            # (or the user has explicitly paired this combo before).
            strict_ok, reason = _spot_short_can_pair(spot, short, spot_price)
            # Hard-skip only on missing data (zero qty, no spot price) —
            # those rows have nothing to display.
            hard_skip = reason in ("no spot price", "zero qty", "zero notional")
            if hard_skip:
                continue
            leg_a, leg_b = _spot_short_pair_decision_keys(
                sym, spot["wallet_id"],
                (short.get("exchange") or "").lower(), short.get("wallet_id") or 0,
            )
            decision = decisions.get((leg_a, leg_b))
            if decision == "unpaired":
                continue  # user explicitly rejected this pair
            avg_entry = avg_entries.get((spot["wallet_id"], sym))
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
                    # Real cost basis from venue trade history when
                    # available — otherwise the frontend falls back to
                    # short.entry as a paired-open approximation.
                    "avg_entry_price": avg_entry,
                },
                "short": short,
                # auto_paired = strict-match passed OR user explicitly
                # confirmed. False values still surface (frontend Sync UI
                # offers them as candidates for manual decision).
                "auto_paired": (decision == "paired") or (decision is None and strict_ok),
                "decision": decision or ("auto" if strict_ok else "candidate"),
                "match_reason": reason,
                "spot_price_estimate": spot_price,
            })
    return out
