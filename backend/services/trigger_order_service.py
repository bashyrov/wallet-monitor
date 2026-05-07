"""Trigger-order monitoring service.

Polls every 1s, evaluates pending arb trigger orders against the current
size-aware effective spread (VWAP from books.json), and fires via atomic
SQL claim-on-fire so cross-replica execution is exactly-once.

Design rationale: see DEV_PROMPT.md §7.2 + §7.6.

Concurrency model: identical pattern to alert_service. Both web replicas
run the loop concurrently — atomic UPDATE…WHERE on `arb_trigger_orders`
guarantees only one wins per trigger, no Redis lease needed.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time as _time
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.db.base import SessionLocal
from backend.db.models import ArbPosition, ArbTriggerOrder, TradePosition, Wallet

logger = logging.getLogger("avalant.trigger_orders")

# ── Tuning ──────────────────────────────────────────────────────────────────
TICK_INTERVAL_S = 1.0
"""How often the loop wakes up. Keep it tight — funding spreads flicker on
200ms scale, 5s misses flash entries. 1s is a good speed-vs-cost balance."""

BOOKS_STALE_MAX_S = 5.0
"""If both legs' orderbooks are older than this, skip the tick. Better to
miss a fire than fire on stale data."""

EXEC_RETRY_TRANSIENT_MS = 200
"""On KindInternal venue error, sleep this long and retry once."""


# ── Spread evaluation ──────────────────────────────────────────────────────
def _vwap_from_levels(levels: list[list[float]], qty: float) -> Optional[float]:
    """Walk orderbook levels accumulating qty until target reached.
    Returns volume-weighted average price. None if insufficient depth.

    `levels` is the format used in books.json: [[price, qty], ...].
    """
    if not levels or qty <= 0:
        return None
    filled = 0.0
    px_qty = 0.0
    for px, lvl_qty in levels:
        if filled >= qty:
            break
        take = min(lvl_qty, qty - filled)
        px_qty += px * take
        filled += take
    if filled < qty * 0.999:  # tolerate 0.1% short-fill (rounding)
        return None
    return px_qty / filled


def _read_book_for_leg(books: dict, exchange: str, symbol: str) -> Optional[dict]:
    """Extract one leg's orderbook from the merged books.json structure.
    Returns {"bids": [...], "asks": [...], "ts": float} or None if missing
    or stale."""
    try:
        # books.json structure: { "<exchange>": { "<symbol>": {"bids":..., "asks":..., "ts":...} } }
        per_ex = books.get(exchange) or {}
        sym_entry = per_ex.get(symbol) or per_ex.get(symbol.upper())
        if not sym_entry:
            return None
        ts = sym_entry.get("ts") or sym_entry.get("timestamp")
        if ts is None:
            return None
        # ts may be ms or seconds — be lenient
        ts_s = ts / 1000.0 if ts > 10**12 else float(ts)
        if (_time.time() - ts_s) > BOOKS_STALE_MAX_S:
            return None
        return sym_entry
    except (AttributeError, TypeError, ValueError):
        return None


def _compute_effective_spread(
    books: dict,
    long_ex: str, long_sym: str,
    short_ex: str, short_sym: str,
    qty_token: float,
) -> Optional[float]:
    """Effective ENTRY spread for opening at `qty_token` size (in_pct).

    Long-side asks (we BUY into asks); short-side bids (we SELL into bids).
    Spread = (short_bid_vwap - long_ask_vwap) / long_ask_vwap × 100.

    Used by `kind='open'` triggers — they fire when the opening spread
    widens past their threshold.

    Returns None if either leg's book is missing/stale or has insufficient
    depth.
    """
    long_book  = _read_book_for_leg(books, long_ex, long_sym)
    short_book = _read_book_for_leg(books, short_ex, short_sym)
    if not long_book or not short_book:
        return None

    long_vwap  = _vwap_from_levels(long_book.get("asks") or [], qty_token)
    short_vwap = _vwap_from_levels(short_book.get("bids") or [], qty_token)
    if not long_vwap or not short_vwap or long_vwap <= 0:
        return None

    return (short_vwap - long_vwap) / long_vwap * 100.0


def _compute_exit_spread(
    books: dict,
    long_ex: str, long_sym: str,
    short_ex: str, short_sym: str,
    qty_token: float,
) -> Optional[float]:
    """Effective EXIT spread for closing at `qty_token` size (out_pct).

    Inverse of the entry path: when closing, we SELL the long leg (hit
    bids) and BUY the short leg back (hit asks). Spread we receive on
    close is therefore (short_ask_vwap - long_bid_vwap) / long_bid_vwap.

    Used by `kind='tp' / 'sl' / 'close'` triggers — they fire on the
    actual fillable close-spread, not the open-side spread (in_pct
    drifts independently and would mis-fire TP/SL on size).

    Returns None on missing/stale book or insufficient depth.
    """
    long_book  = _read_book_for_leg(books, long_ex, long_sym)
    short_book = _read_book_for_leg(books, short_ex, short_sym)
    if not long_book or not short_book:
        return None

    long_vwap  = _vwap_from_levels(long_book.get("bids") or [], qty_token)
    short_vwap = _vwap_from_levels(short_book.get("asks") or [], qty_token)
    if not long_vwap or not short_vwap or long_vwap <= 0:
        return None

    return (short_vwap - long_vwap) / long_vwap * 100.0


def _spread_for_order(
    books: dict, order: "ArbTriggerOrder", qty_token: float,
) -> Optional[float]:
    """Pick the right spread direction for this trigger kind.
       open  → in_pct  (entry side: long asks, short bids)
       tp/sl → out_pct (exit side: long bids, short asks)
       close → out_pct (same as tp/sl)
    """
    long_ex  = order.long_exchange
    long_sym = order.long_symbol
    short_ex = order.short_exchange
    short_sym = order.short_symbol
    if order.kind == "open":
        return _compute_effective_spread(books, long_ex, long_sym, short_ex, short_sym, qty_token)
    return _compute_exit_spread(books, long_ex, long_sym, short_ex, short_sym, qty_token)


def _load_books_json() -> Optional[dict]:
    """Read the Go-fetcher merged orderbook cache."""
    import json
    import os
    cache_dir = os.environ.get("AVALANT_FETCHER_CACHE_DIR", "/tmp/avalant_cache")
    path = os.path.join(cache_dir, "books.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ── Condition evaluation ───────────────────────────────────────────────────
def condition_met(order: ArbTriggerOrder, current_spread: float) -> bool:
    """All thresholds are absolute spread % (no relative mode in v1).

    'open' / 'sl' fire when spread WIDENS past the threshold (>=).
    'close' / 'tp' fire when spread CONVERGES below the threshold (<=).
    """
    if order.trigger_spread_pct is None:
        return True   # market trigger — fire next tick
    if order.kind in ("open", "sl"):
        return current_spread >= order.trigger_spread_pct
    if order.kind in ("close", "tp"):
        return current_spread <= order.trigger_spread_pct
    return False


# ── VWAP merge for accumulated entry prices ────────────────────────────────
def vwap_merge(prev_price: Optional[float], prev_qty: float,
               new_price: float, new_qty: float) -> float:
    """Weighted-average two prices. Used when accumulating portions into
    the running entry_price on arb_position."""
    prev_qty = prev_qty or 0.0
    if prev_qty <= 0 or prev_price is None:
        return new_price
    total = prev_qty + new_qty
    if total <= 0:
        return new_price
    return (prev_price * prev_qty + new_price * new_qty) / total


# ── Atomic claim ────────────────────────────────────────────────────────────
def claim_for_fire(db: Session, order_id: int) -> bool:
    """Atomically transition `pending` → `firing` for exactly-once execution
    across replicas. Returns True if this caller won the race.

    Postgres + SQLite both honor the WHERE clause as a row-lock predicate.
    """
    rows = db.execute(
        text(
            "UPDATE arb_trigger_orders "
            "SET status = 'firing', updated_at = :now "
            "WHERE id = :id AND status = 'pending'"
        ),
        {"id": order_id, "now": datetime.utcnow()},
    ).rowcount
    db.commit()
    return rows == 1


# ── Position accumulation ───────────────────────────────────────────────────
def accumulate_position(
    db: Session, trigger: ArbTriggerOrder,
    long_fill_price: float, long_fill_qty: float,
    short_fill_price: float, short_fill_qty: float,
) -> ArbPosition:
    """Fold one portion-fill result into the parent arb_position.
    Creates the position row on first fill; VWAP-merges thereafter."""
    # Note: read arb_position_id directly so we still find the row on
    # subsequent portions even when SQLAlchemy hasn't refreshed the
    # relationship cache (happens within a single session after a flush).
    pos = None
    if trigger.arb_position_id is not None:
        from backend.db.base import SessionLocal as _SL  # noqa: F401
        pos = db.query(ArbPosition).filter(ArbPosition.id == trigger.arb_position_id).first()
    if pos is None:
        pos = ArbPosition(
            user_id=trigger.user_id,
            kind="long_short",  # default; spot_short callers override on create
            long_exchange=trigger.long_exchange or "",
            long_symbol=trigger.long_symbol or "",
            long_wallet_id=trigger.long_wallet_id,
            short_exchange=trigger.short_exchange or "",
            short_symbol=trigger.short_symbol or "",
            short_wallet_id=trigger.short_wallet_id,
            target_qty_token=trigger.total_qty_token,
            leverage=trigger.leverage,
            margin_mode=trigger.margin_mode,
            status="open",
            opened_at=datetime.utcnow(),
            long_qty=0.0, short_qty=0.0,
        )
        db.add(pos)
        db.flush()
        trigger.arb_position_id = pos.id

    pos.long_entry_price = vwap_merge(
        pos.long_entry_price, pos.long_qty, long_fill_price, long_fill_qty
    )
    pos.short_entry_price = vwap_merge(
        pos.short_entry_price, pos.short_qty, short_fill_price, short_fill_qty
    )
    pos.long_qty = (pos.long_qty or 0.0) + long_fill_qty
    pos.short_qty = (pos.short_qty or 0.0) + short_fill_qty
    if pos.long_entry_price and pos.long_entry_price > 0:
        pos.entry_spread_pct = (
            (pos.short_entry_price - pos.long_entry_price)
            / pos.long_entry_price * 100.0
        )
    if pos.opened_at is None:
        pos.opened_at = datetime.utcnow()
    pos.status = "open"
    pos.updated_at = datetime.utcnow()
    return pos


# ── Auto-pair detection (called from trade_service after every fill) ───────
SYMBOL_PAIR_NOTIONAL_TOLERANCE = 0.12   # ±12%, mirror of spot-short
PAIR_TIME_WINDOW_S = 600                # ±10 min


def auto_pair_internal_legs(db: Session, user_id: int) -> list[ArbPosition]:
    """Scan unwrapped TradePosition rows (arb_position_id IS NULL) and pair
    them with their mirror leg if one exists.

    Match criteria:
      - same symbol_normalized
      - opposite side (one buy, one sell)
      - on different exchanges
      - notional within ±12%
      - opened within ±10 min of each other

    Returns list of newly-created arb_positions.
    """
    open_unwrapped = (
        db.query(TradePosition)
        .filter(
            TradePosition.user_id == user_id,
            TradePosition.arb_position_id.is_(None),
            TradePosition.status == "open",
            TradePosition.kind == "single",
        )
        .all()
    )
    by_symbol: dict[str, list[TradePosition]] = {}
    for p in open_unwrapped:
        by_symbol.setdefault((p.symbol or "").upper(), []).append(p)

    created: list[ArbPosition] = []
    for sym, positions in by_symbol.items():
        # Try every pair within the symbol bucket
        for i in range(len(positions)):
            a = positions[i]
            if a.arb_position_id is not None:
                continue
            for j in range(i + 1, len(positions)):
                b = positions[j]
                if b.arb_position_id is not None:
                    continue
                if a.leg_a_side == b.leg_a_side:
                    continue                    # same direction, not a pair
                if a.leg_a_exchange == b.leg_a_exchange:
                    continue                    # same venue, not a pair
                # Notional tolerance check (using qty as a proxy if prices
                # aren't filled yet — both should be unless mid-flight)
                a_notional = (a.leg_a_entry_price or 0) * (a.leg_a_qty or 0)
                b_notional = (b.leg_a_entry_price or 0) * (b.leg_a_qty or 0)
                if a_notional <= 0 or b_notional <= 0:
                    continue
                avg = (a_notional + b_notional) / 2
                if abs(a_notional - b_notional) / avg > SYMBOL_PAIR_NOTIONAL_TOLERANCE:
                    continue
                # Time-window check
                if abs((a.opened_at - b.opened_at).total_seconds()) > PAIR_TIME_WINDOW_S:
                    continue

                # Determine long vs short
                if a.leg_a_side == "buy":
                    long_p, short_p = a, b
                else:
                    long_p, short_p = b, a

                pos = ArbPosition(
                    user_id=user_id,
                    kind="long_short",
                    long_exchange=long_p.leg_a_exchange,
                    long_symbol=long_p.symbol,
                    long_wallet_id=long_p.leg_a_wallet_id,
                    short_exchange=short_p.leg_a_exchange,
                    short_symbol=short_p.symbol,
                    short_wallet_id=short_p.leg_a_wallet_id,
                    long_entry_price=long_p.leg_a_entry_price,
                    short_entry_price=short_p.leg_a_entry_price,
                    long_qty=long_p.leg_a_qty,
                    short_qty=short_p.leg_a_qty,
                    opened_at=min(long_p.opened_at, short_p.opened_at),
                    status="open",
                    synced_externally=False,
                )
                if pos.long_entry_price and pos.long_entry_price > 0:
                    pos.entry_spread_pct = (
                        (pos.short_entry_price - pos.long_entry_price)
                        / pos.long_entry_price * 100.0
                    )
                db.add(pos)
                db.flush()
                long_p.arb_position_id = pos.id
                short_p.arb_position_id = pos.id
                created.append(pos)
                logger.info(
                    "auto-paired %s: long=%s, short=%s → arb_position id=%d",
                    sym, long_p.leg_a_exchange, short_p.leg_a_exchange, pos.id,
                )
                break  # a is paired now
    if created:
        db.commit()
    return created


# ── Main loop ──────────────────────────────────────────────────────────────
async def _tick(db: Session, books: Optional[dict]) -> None:
    """One tick of the trigger evaluation loop."""
    # Promote scheduled → pending if activate_at reached
    db.execute(
        text(
            "UPDATE arb_trigger_orders "
            "SET status = 'pending', updated_at = :now "
            "WHERE status = 'scheduled' AND activate_at IS NOT NULL "
            "AND activate_at <= :now"
        ),
        {"now": datetime.utcnow()},
    )
    db.commit()

    if books is None:
        return  # nothing to evaluate without orderbooks

    pending = (
        db.query(ArbTriggerOrder)
        .filter(ArbTriggerOrder.status == "pending")
        .all()
    )
    for order in pending:
        # Determine size for VWAP — portion if set, otherwise total
        qty = order.portion_size_token or order.total_qty_token
        if not qty or qty <= 0:
            continue
        long_ex = order.long_exchange
        long_sym = order.long_symbol
        short_ex = order.short_exchange
        short_sym = order.short_symbol
        # For TP/SL, inherit from parent arb_position
        if order.kind in ("tp", "sl") and order.arb_position is not None:
            long_ex = long_ex or order.arb_position.long_exchange
            long_sym = long_sym or order.arb_position.long_symbol
            short_ex = short_ex or order.arb_position.short_exchange
            short_sym = short_sym or order.arb_position.short_symbol
            qty = order.arb_position.long_qty or qty
        if not long_ex or not short_ex:
            continue
        # Direction depends on kind: open uses in_pct, tp/sl/close use
        # out_pct. Without this split, TP/SL would chase the open-spread
        # which doesn't reflect what we'd actually receive on close.
        if order.kind == "open":
            spread = _compute_effective_spread(
                books, long_ex, long_sym, short_ex, short_sym, qty,
            )
        else:
            spread = _compute_exit_spread(
                books, long_ex, long_sym, short_ex, short_sym, qty,
            )
        if spread is None:
            continue
        if not condition_met(order, spread):
            continue
        if not claim_for_fire(db, order.id):
            continue
        # Re-fetch under the firing lock so we have a fresh view
        db.refresh(order)
        try:
            await _execute_portion(db, order, spread)
        except Exception as e:    # noqa: BLE001
            logger.exception("trigger fire %d failed: %s", order.id, e)
            order.status = "failed"
            order.error_kind = "internal"
            order.error_message = str(e)[:400]
            order.updated_at = datetime.utcnow()
            db.commit()


async def _execute_portion(db: Session, order: ArbTriggerOrder, snapshot_spread: float) -> None:
    """Fire ONE portion of the trigger. Wires through trade_service for the
    actual venue calls. Updates portions_filled and re-arms / finalizes per
    the state machine.
    """
    from backend.services import trade_service

    if order.kind in ("open",):
        await _execute_open_portion(db, order, trade_service, snapshot_spread)
    elif order.kind in ("close", "tp", "sl"):
        await _execute_close(db, order, trade_service, snapshot_spread)
    else:
        order.status = "failed"
        order.error_kind = "internal"
        order.error_message = f"unknown trigger kind: {order.kind}"
        db.commit()


async def _execute_open_portion(
    db: Session, order: ArbTriggerOrder, trade_service, snapshot_spread: float,
) -> None:
    qty = order.portion_size_token or order.total_qty_token
    if not qty:
        order.status = "failed"
        order.error_kind = "user"
        order.error_message = "trigger has no qty"
        db.commit()
        return

    # Resolve wallets (fail soft if missing — happens after wallet delete)
    long_w = db.query(Wallet).filter(Wallet.id == order.long_wallet_id).first() if order.long_wallet_id else None
    short_w = db.query(Wallet).filter(Wallet.id == order.short_wallet_id).first() if order.short_wallet_id else None
    if not long_w or not short_w:
        order.status = "failed"
        order.error_kind = "user"
        order.error_message = "wallet missing"
        db.commit()
        return

    leverage = order.leverage or 1
    margin_mode = order.margin_mode or "isolated"

    async def _open_long():
        return await trade_service.place_open_order(
            db, order.user_id, long_w.id, order.long_symbol or "",
            "buy", qty, leverage, margin_mode,
        )

    async def _open_short():
        return await trade_service.place_open_order(
            db, order.user_id, short_w.id, order.short_symbol or "",
            "sell", qty, leverage, margin_mode,
        )

    long_res, short_res = await asyncio.gather(_open_long(), _open_short(), return_exceptions=True)

    long_ok = not isinstance(long_res, Exception) and isinstance(long_res, dict)
    short_ok = not isinstance(short_res, Exception) and isinstance(short_res, dict)

    if long_ok and short_ok:
        accumulate_position(
            db, order,
            long_fill_price=float(long_res.get("avg_fill_price") or long_res.get("price") or 0),
            long_fill_qty=float(long_res.get("filled_qty") or qty),
            short_fill_price=float(short_res.get("avg_fill_price") or short_res.get("price") or 0),
            short_fill_qty=float(short_res.get("filled_qty") or qty),
        )
        order.portions_filled += 1
        order.last_fired_at = datetime.utcnow()
        _notify(order.user_id)

        if order.infinite_fill:
            order.status = "pending"  # re-arm
        elif order.portions_filled >= (order.portions_target or 1):
            order.status = "fired"
            # Promote child TP/SL from scheduled → pending now that position exists
            db.execute(
                text(
                    "UPDATE arb_trigger_orders "
                    "SET status = 'pending', updated_at = :now "
                    "WHERE parent_trigger_id = :pid AND status = 'scheduled'"
                ),
                {"pid": order.id, "now": datetime.utcnow()},
            )
        else:
            order.status = "pending"  # more portions to go
        order.updated_at = datetime.utcnow()
        db.commit()
        logger.info("portion fired: trigger=%d filled=%d/%s spread=%.4f%%",
                    order.id, order.portions_filled, order.portions_target, snapshot_spread)
        return

    # One or both legs failed
    err_msgs = []
    if not long_ok:
        err_msgs.append(f"long: {_short_err(long_res)}")
    if not short_ok:
        err_msgs.append(f"short: {_short_err(short_res)}")

    order.status = "failed"
    order.error_kind = "partial" if (long_ok or short_ok) else "exchange"
    order.error_message = "; ".join(err_msgs)[:400]
    order.last_fired_at = datetime.utcnow()
    order.updated_at = datetime.utcnow()
    _notify(order.user_id)
    if long_ok and not short_ok:
        # Long leg created — wrap in a partial arb_position so user can see it
        accumulate_position(
            db, order,
            long_fill_price=float(long_res.get("avg_fill_price") or 0),
            long_fill_qty=float(long_res.get("filled_qty") or qty),
            short_fill_price=0.0, short_fill_qty=0.0,
        )
        if order.arb_position is not None:
            order.arb_position.status = "partial"
    elif short_ok and not long_ok:
        accumulate_position(
            db, order,
            long_fill_price=0.0, long_fill_qty=0.0,
            short_fill_price=float(short_res.get("avg_fill_price") or 0),
            short_fill_qty=float(short_res.get("filled_qty") or qty),
        )
        if order.arb_position is not None:
            order.arb_position.status = "partial"
    db.commit()
    logger.warning("trigger %d failed: %s", order.id, order.error_message)


async def _execute_close(
    db: Session, order: ArbTriggerOrder, trade_service, snapshot_spread: float,
) -> None:
    pos = order.arb_position
    if pos is None or pos.status not in ("open", "partial"):
        order.status = "failed"
        order.error_kind = "user"
        order.error_message = "no open position to close"
        db.commit()
        return

    long_w_id = pos.long_wallet_id
    short_w_id = pos.short_wallet_id
    if not long_w_id or not short_w_id:
        order.status = "failed"
        order.error_kind = "user"
        order.error_message = "position has no wallet refs"
        db.commit()
        return

    pos.status = "closing"
    db.commit()

    async def _close_long():
        return await trade_service.close_position(
            db, order.user_id, long_w_id, pos.long_symbol, "sell",
        )

    async def _close_short():
        return await trade_service.close_position(
            db, order.user_id, short_w_id, pos.short_symbol, "buy",
        )

    long_res, short_res = await asyncio.gather(_close_long(), _close_short(), return_exceptions=True)
    long_ok = not isinstance(long_res, Exception)
    short_ok = not isinstance(short_res, Exception)

    if long_ok and short_ok:
        pos.status = "closed"
        pos.closed_at = datetime.utcnow()
        if isinstance(long_res, dict):
            pos.long_exit_price = float(long_res.get("avg_fill_price") or 0)
        if isinstance(short_res, dict):
            pos.short_exit_price = float(short_res.get("avg_fill_price") or 0)
        if pos.long_exit_price and pos.long_exit_price > 0 and pos.short_exit_price:
            pos.exit_spread_pct = (
                (pos.short_exit_price - pos.long_exit_price)
                / pos.long_exit_price * 100.0
            )
        order.status = "fired"
        order.last_fired_at = datetime.utcnow()
        # Cancel sibling triggers (the other of TP/SL won't fire on a closed pos)
        db.execute(
            text(
                "UPDATE arb_trigger_orders "
                "SET status = 'cancelled', updated_at = :now "
                "WHERE arb_position_id = :pid AND status IN ('pending','scheduled') "
                "AND id != :self_id"
            ),
            {"pid": pos.id, "self_id": order.id, "now": datetime.utcnow()},
        )
        # Cancel any unfilled open-trigger portions on this arb pair
        db.execute(
            text(
                "UPDATE arb_trigger_orders "
                "SET status = 'cancelled', updated_at = :now "
                "WHERE arb_position_id = :pid AND kind = 'open' AND status = 'pending'"
            ),
            {"pid": pos.id, "now": datetime.utcnow()},
        )
    else:
        # Partial close — leave position alive, mark trigger failed
        order.status = "failed"
        order.error_kind = "partial" if (long_ok or short_ok) else "exchange"
        err_msgs = []
        if not long_ok:
            err_msgs.append(f"long-close: {_short_err(long_res)}")
        if not short_ok:
            err_msgs.append(f"short-close: {_short_err(short_res)}")
        order.error_message = "; ".join(err_msgs)[:400]
        if long_ok or short_ok:
            pos.status = "partial"
        else:
            pos.status = "open"   # nothing changed; re-open
    order.updated_at = datetime.utcnow()
    db.commit()
    _notify(order.user_id)


def _short_err(res) -> str:
    if isinstance(res, Exception):
        return str(res)[:150]
    if isinstance(res, dict):
        return str(res.get("error") or res.get("message") or res)[:150]
    return str(res)[:150]


def _notify(user_id: int) -> None:
    """Schedule a WS push to /ws/positions clients of `user_id`. Lazy
    import to avoid the screener module pulling in trigger service deps
    at import time."""
    try:
        from backend.api.v1.screener import notify_position_update
        notify_position_update(user_id)
    except Exception:
        pass


# ── Daemon loop ─────────────────────────────────────────────────────────────
_loop_task: Optional[asyncio.Task] = None


async def _run_loop() -> None:
    """Daemon entry point. Runs forever — atomic claim handles cross-replica
    coordination so it's safe to start on every web replica."""
    while True:
        try:
            books = _load_books_json()
            db = SessionLocal()
            try:
                await _tick(db, books)
            finally:
                db.close()
        except Exception:
            logger.exception("trigger_order_service: tick failed")
        await asyncio.sleep(TICK_INTERVAL_S)


def start() -> None:
    """Start the daemon loop. Called once at app startup from inside the
    FastAPI lifespan (which runs in the running event loop). Use the
    modern get_running_loop API — get_event_loop() is deprecated when
    called outside a coroutine in Python 3.12+ and silently fails the
    way it was being used here previously."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("trigger_order_service.start(): no running event loop")
        return
    _loop_task = loop.create_task(_run_loop(), name="trigger_order_loop")
    logger.info("trigger_order_service started (tick=%ss)", TICK_INTERVAL_S)
