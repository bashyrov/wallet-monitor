"""Position reconciliation worker.

Runs on the fetcher container, every 60 seconds. For each user with
trade-enabled wallets, diff the current live position set against the
last known set in `trade_positions` and:

  · NEW position → insert TradePosition(kind=single, status=open).
                   If a recent matching trade_orders(intent=open, filled)
                   row exists, link it. Otherwise mark opened_externally.

  · STILL OPEN  → refresh leg_a_qty, leg_a_entry_price, leg_a_funding_pnl
                  from the live snapshot (positions evolve via DCA).

  · DISAPPEARED → set status=closed, closed_at=now(),
                  leg_a_exit_price = best-effort from last known mark.
                  closed_externally=True if no recent matching close order.

Pair stitching is NOT done here — the P&L tab applies the auto-pair rule
and decision overrides at read time, grouping closed singles into pairs
when applicable. That keeps the reconcile logic dialog-free.

Per-exchange fuse: if `list_user_positions` raises for a venue, that
venue's wallets are skipped this cycle but the rest of the user's wallets
keep reconciling. We rely on `list_user_positions`' own per-wallet
last-good cache to mask transient blips.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.db.base import SessionLocal
from backend.db.models import User, Wallet, TradePosition, TradeOrder
from backend.services import trade_service

logger = logging.getLogger("avalant.reconcile")

# 5-minute reconcile cycle. WS user-streams (11 venues) handle live
# position changes; reconcile is now a safety net for events the WS
# missed (subscribe-time race, brief disconnect, externally-opened
# positions on exchanges that don't push diffs). 60s was the right
# cadence when REST was the only source — now it's overkill and
# generates needless API load.
_LOOP_INTERVAL_S = 300.0
_thread: threading.Thread | None = None
_stop = threading.Event()
# Match window for linking a new trade_position to a recent trade_orders
# row that placed it. Anything older than this is treated as
# "opened externally" — the user opened it on the exchange UI directly.
_OPEN_LINK_WINDOW_S = 600
_CLOSE_LINK_WINDOW_S = 600


def _users_with_trade_wallets(db: Session) -> list[int]:
    """Return user_ids that have at least one screener / both purpose
    exchange or perpdex wallet — only those need reconciliation."""
    rows = (
        db.query(Wallet.user_id)
        .filter(
            Wallet.wallet_type.in_(("exchange", "perpdex")),
            Wallet.purpose.in_(("screener", "both")),
            Wallet.is_archived == False,  # noqa: E712
        )
        .distinct()
        .all()
    )
    return [r[0] for r in rows if r[0] is not None]


def _fingerprint(p: dict) -> tuple[int, str, str]:
    """Per-position dedup key. Stable across DCA / partial fills since
    we don't include qty or entry_price."""
    return (int(p.get("wallet_id") or 0), str(p.get("symbol") or "").upper(), str(p.get("side") or "").lower())


def _link_recent_open_order(db: Session, user_id: int, wallet_id: int,
                             symbol: str, side: str) -> int | None:
    cutoff = datetime.utcnow() - timedelta(seconds=_OPEN_LINK_WINDOW_S)
    row = (
        db.query(TradeOrder)
        .filter(
            TradeOrder.user_id == user_id,
            TradeOrder.wallet_id == wallet_id,
            TradeOrder.symbol == symbol.upper(),
            TradeOrder.side == side.lower(),
            TradeOrder.intent == "open",
            TradeOrder.status == "filled",
            TradeOrder.position_id.is_(None),
            TradeOrder.created_at >= cutoff,
        )
        .order_by(TradeOrder.created_at.desc())
        .first()
    )
    return row.id if row else None


def _link_recent_close_order(db: Session, user_id: int, wallet_id: int,
                              symbol: str) -> int | None:
    cutoff = datetime.utcnow() - timedelta(seconds=_CLOSE_LINK_WINDOW_S)
    row = (
        db.query(TradeOrder)
        .filter(
            TradeOrder.user_id == user_id,
            TradeOrder.wallet_id == wallet_id,
            TradeOrder.symbol == symbol.upper(),
            TradeOrder.intent == "close",
            TradeOrder.status == "filled",
            TradeOrder.created_at >= cutoff,
        )
        .order_by(TradeOrder.created_at.desc())
        .first()
    )
    return row.id if row else None


async def _reconcile_user(user_id: int) -> tuple[int, int, int]:
    """Reconcile one user. Returns (opens_created, closes_marked, still_open)
    so the worker can log a cycle summary."""
    opens_created = 0
    closes_marked = 0
    still_open = 0
    db = SessionLocal()
    try:
        try:
            live = await trade_service.list_user_positions(db, user_id)
        except Exception as exc:
            logger.info("reconcile: list_user_positions failed user=%s: %s", user_id, exc)
            return (0, 0, 0)

        live_by_fp: dict[tuple[int, str, str], dict] = {}
        for p in live:
            fp = _fingerprint(p)
            if fp[0] == 0:
                continue  # missing wallet_id
            live_by_fp[fp] = p

        # All TradePosition rows we currently consider OPEN for this user.
        open_rows: list[TradePosition] = (
            db.query(TradePosition)
            .filter(
                TradePosition.user_id == user_id,
                TradePosition.status == "open",
                TradePosition.kind == "single",
            )
            .all()
        )

        seen_fps: set[tuple[int, str, str]] = set()
        # 1) update / close existing
        for row in open_rows:
            fp = (row.leg_a_wallet_id or 0, (row.symbol or "").upper(), (row.leg_a_side or "").lower())
            seen_fps.add(fp)
            live_p = live_by_fp.get(fp)
            if live_p:
                still_open += 1
                # Still open — refresh evolving fields.
                row.leg_a_qty = float(live_p.get("quantity") or row.leg_a_qty or 0)
                ep = live_p.get("entry_price")
                if ep is not None:
                    try:
                        row.leg_a_entry_price = float(ep)
                    except (TypeError, ValueError):
                        pass
                fp_pnl = live_p.get("funding_pnl_usd")
                if fp_pnl is not None:
                    try:
                        row.leg_a_funding_pnl_usd = float(fp_pnl)
                    except (TypeError, ValueError):
                        pass
            else:
                # Disappeared from live → closed.
                closes_marked += 1
                row.status = "closed"
                row.closed_at = datetime.utcnow()
                logger.info(
                    "reconcile: position CLOSED user=%s ex=%s sym=%s side=%s qty=%s entry=%s",
                    user_id, row.leg_a_exchange, row.symbol, row.leg_a_side,
                    row.leg_a_qty, row.leg_a_entry_price,
                )
                # Approximate exit using last-known mark price if available.
                # Stage 2c will fetch the precise realized PnL from the
                # exchange's closed-trades endpoint.
                close_oid = _link_recent_close_order(
                    db, user_id, row.leg_a_wallet_id, row.symbol or ""
                )
                if close_oid:
                    row.leg_a_close_order_id = close_oid
                else:
                    row.closed_externally = True
                # Best-effort realized PnL from entry/last-mark difference.
                # Without a precise exit price this is approximate — Stage 2c
                # replaces this with the exchange-reported realized PnL.
                if row.leg_a_entry_price and row.leg_a_qty:
                    sign = 1.0 if (row.leg_a_side or "").lower() == "buy" else -1.0
                    # If we have a stored exit_price already (e.g. from
                    # close order), use it. Otherwise leave NULL — caller
                    # treats NULL as "unknown" rather than $0.
                    if row.leg_a_exit_price:
                        row.leg_a_realized_pnl_usd = sign * (row.leg_a_exit_price - row.leg_a_entry_price) * row.leg_a_qty
                row.realized_pnl_usd = row.leg_a_realized_pnl_usd

        # 2) insert new positions
        new_fps = set(live_by_fp.keys()) - seen_fps
        for fp in new_fps:
            live_p = live_by_fp[fp]
            wallet_id, symbol, side = fp
            entry_price = live_p.get("entry_price")
            try:
                entry_price_f = float(entry_price) if entry_price is not None else None
            except (TypeError, ValueError):
                entry_price_f = None
            qty = float(live_p.get("quantity") or 0)
            open_oid = _link_recent_open_order(db, user_id, wallet_id, symbol, side)
            row = TradePosition(
                user_id=user_id,
                kind="single",
                status="open",
                symbol=symbol,
                leg_a_wallet_id=wallet_id,
                leg_a_exchange=str(live_p.get("exchange") or "").lower(),
                leg_a_side=side,
                leg_a_qty=qty,
                leg_a_entry_price=entry_price_f,
                leg_a_open_order_id=open_oid,
                opened_externally=open_oid is None,
            )
            db.add(row)
            opens_created += 1
            if open_oid:
                # Backlink the order so Order History can show its position.
                ord_row = db.query(TradeOrder).filter(TradeOrder.id == open_oid).first()
                if ord_row and ord_row.position_id is None:
                    db.flush()  # populate row.id
                    ord_row.position_id = row.id
            logger.info(
                "reconcile: position OPENED user=%s ex=%s sym=%s side=%s qty=%s entry=%s source=%s",
                user_id, row.leg_a_exchange, symbol, side, qty, entry_price_f,
                "ours" if open_oid else "exchange",
            )
        db.commit()

        # ── arb_positions reconcile pass ──────────────────────────────────
        # After trade_positions are settled, walk the user's open/partial
        # arb_positions and:
        #   - finalize them when both legs are closed externally (pull entry/
        #     exit prices and realized P&L from venue state if available)
        #   - mark partial when only one leg closes externally
        #   - downstream cascade-cancel any pending TP/SL children
        # Then auto-pair any newly-orphaned trade_positions into arb_positions
        # so the user sees them as paired entities in the Positions tab.
        try:
            await _reconcile_arb_positions(db, user_id, live_by_fp)
        except Exception:
            logger.exception("reconcile_arb_positions failed user=%s", user_id)

        return (opens_created, closes_marked, still_open)
    finally:
        db.close()


async def _reconcile_arb_positions(db: Session, user_id: int,
                                    live_by_fp: dict[tuple[int, str, str], dict]) -> None:
    """Walk the user's open/partial arb_positions and reconcile their
    state against current venue positions. See DEV_PROMPT.md §7.6.B.

    `live_by_fp` is the per-leg fingerprint→live-position map already built
    by the caller — reuse it for free instead of re-querying every venue.
    """
    from datetime import datetime as _dt
    from sqlalchemy import text as _text
    from backend.db.models import ArbPosition, ArbTriggerOrder, TradePosition
    from backend.services.trigger_order_service import auto_pair_internal_legs

    open_arbs: list[ArbPosition] = (
        db.query(ArbPosition)
        .filter(
            ArbPosition.user_id == user_id,
            ArbPosition.status.in_(("open", "partial", "closing")),
        )
        .all()
    )

    for ap in open_arbs:
        # Look up each leg's live state by (wallet_id, symbol, side)
        long_fp  = (ap.long_wallet_id  or 0, (ap.long_symbol  or "").upper(), "buy")
        short_fp = (ap.short_wallet_id or 0, (ap.short_symbol or "").upper(), "sell")
        live_long  = live_by_fp.get(long_fp)
        live_short = live_by_fp.get(short_fp)

        long_qty_live  = float((live_long  or {}).get("quantity") or 0)
        short_qty_live = float((live_short or {}).get("quantity") or 0)

        long_closed  = (long_qty_live  == 0) and (ap.long_qty  or 0) > 0
        short_closed = (short_qty_live == 0) and (ap.short_qty or 0) > 0

        if long_closed and short_closed:
            # Externally closed on both sides — finalize.
            ap.status = "closed"
            ap.closed_externally = True
            ap.closed_at = _dt.utcnow()
            # Pull final exit prices + realized PnL from the wrapped
            # trade_positions (reconcile-pass already populated those).
            children: list[TradePosition] = (
                db.query(TradePosition)
                .filter(TradePosition.arb_position_id == ap.id)
                .all()
            )
            realized_sum = 0.0
            funding_sum = 0.0
            long_exit = None
            short_exit = None
            for ch in children:
                if ch.leg_a_side == "buy" and ch.leg_a_exit_price:
                    long_exit = ch.leg_a_exit_price
                elif ch.leg_a_side == "sell" and ch.leg_a_exit_price:
                    short_exit = ch.leg_a_exit_price
                realized_sum += float(ch.leg_a_realized_pnl_usd or 0) + float(ch.leg_b_realized_pnl_usd or 0)
                funding_sum  += float(ch.leg_a_funding_pnl_usd  or 0) + float(ch.leg_b_funding_pnl_usd  or 0)
            if long_exit  is not None: ap.long_exit_price  = long_exit
            if short_exit is not None: ap.short_exit_price = short_exit
            if ap.long_exit_price and ap.long_exit_price > 0 and ap.short_exit_price:
                ap.exit_spread_pct = (
                    (ap.short_exit_price - ap.long_exit_price)
                    / ap.long_exit_price * 100.0
                )
            ap.realized_pnl_usd = realized_sum + funding_sum
            ap.updated_at = _dt.utcnow()
            # Cascade-cancel any pending TP/SL children that haven't fired.
            db.execute(
                _text(
                    "UPDATE arb_trigger_orders "
                    "SET status='cancelled', error_kind='position_closed_externally', "
                    "    error_message='reconcile detected external close', "
                    "    updated_at=:now "
                    "WHERE arb_position_id=:pid AND status IN ('pending','firing','scheduled')"
                ),
                {"pid": ap.id, "now": _dt.utcnow()},
            )
            logger.info(
                "reconcile: arb_position %d → closed externally (user=%s, %s ↔ %s, P&L=%.2f USDT)",
                ap.id, user_id, ap.long_exchange, ap.short_exchange, ap.realized_pnl_usd or 0,
            )
        elif long_closed or short_closed:
            # One leg gone, other still alive → partial.
            if ap.status != "partial":
                ap.status = "partial"
                ap.updated_at = _dt.utcnow()
                logger.info(
                    "reconcile: arb_position %d → partial (long_qty=%s short_qty=%s, user=%s)",
                    ap.id, long_qty_live, short_qty_live, user_id,
                )
            # Sync remaining qty so the UI reflects the partial state.
            # The closed leg has live_qty=0 (or is missing from live_by_fp);
            # zero out our copy explicitly so the UI doesn't show stale qty.
            if long_closed:  ap.long_qty  = 0.0
            elif live_long  is not None: ap.long_qty  = long_qty_live
            if short_closed: ap.short_qty = 0.0
            elif live_short is not None: ap.short_qty = short_qty_live
        else:
            # Both legs alive — sync qty in case venue is now larger
            # (user added externally) or smaller (partial close not yet
            # zero'd one leg).
            if live_long  is not None and abs(long_qty_live  - (ap.long_qty  or 0)) > 1e-9:
                ap.long_qty  = long_qty_live
                ap.updated_at = _dt.utcnow()
            if live_short is not None and abs(short_qty_live - (ap.short_qty or 0)) > 1e-9:
                ap.short_qty = short_qty_live
                ap.updated_at = _dt.utcnow()

    # Auto-pair any unwrapped TradePosition rows. Runs after we update
    # arb_positions so already-wrapped legs are skipped.
    try:
        auto_pair_internal_legs(db, user_id)
    except Exception:
        logger.exception("auto_pair_internal_legs failed user=%s", user_id)
    db.commit()

    # Push a refresh event if anything changed in arb_positions or
    # auto-paired wrappers were created.
    if open_arbs:
        try:
            from backend.api.v1.screener import notify_position_update
            notify_position_update(user_id)
        except Exception:
            pass


_RECONCILE_CONCURRENCY = 4


async def _reconcile_pass() -> None:
    db = SessionLocal()
    try:
        user_ids = _users_with_trade_wallets(db)
    finally:
        db.close()
    if not user_ids:
        return

    # Bounded-concurrency reconcile. Each user holds onto its own SessionLocal
    # for the duration of its reconcile_user() call, and concurrent users hit
    # different exchange API keys so per-key rate limits don't compound. The
    # ceiling (4) is a balance: high enough that 50-100 users finish well
    # under the 60s budget, low enough that 8 different users * 8 exchanges
    # per user doesn't pulse the network too hard.
    sem = asyncio.Semaphore(_RECONCILE_CONCURRENCY)
    counters = {"opens": 0, "closes": 0, "open": 0, "failed": 0}

    async def _bounded(uid: int) -> None:
        if _stop.is_set():
            return
        async with sem:
            try:
                opens, closes, still_open = await _reconcile_user(uid)
                counters["opens"] += opens
                counters["closes"] += closes
                counters["open"] += still_open
            except Exception as exc:
                counters["failed"] += 1
                logger.exception("reconcile user=%s failed: %s", uid, exc)

    await asyncio.gather(*(_bounded(uid) for uid in user_ids))

    logger.info(
        "reconcile pass: users=%d still_open=%d new_opens=%d new_closes=%d failed_users=%d (concurrency=%d)",
        len(user_ids), counters["open"], counters["opens"], counters["closes"],
        counters["failed"], _RECONCILE_CONCURRENCY,
    )


def _runner() -> None:
    logger.info("reconcile worker started (cycle=%ss)", _LOOP_INTERVAL_S)
    while not _stop.is_set():
        t0 = time.time()
        try:
            asyncio.run(_reconcile_pass())
        except Exception as exc:
            logger.exception("reconcile pass failed: %s", exc)
        elapsed = time.time() - t0
        sleep_for = max(5.0, _LOOP_INTERVAL_S - elapsed)
        # Don't busy-sleep through stop; check the event regularly.
        end = time.time() + sleep_for
        while time.time() < end and not _stop.is_set():
            time.sleep(min(2.0, end - time.time()))
    logger.info("reconcile worker stopped")


def start_reconcile_service() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_runner, name="reconcile-worker", daemon=True)
    _thread.start()


def stop_reconcile_service() -> None:
    _stop.set()
