"""Fills backfill service.

Pulls recent fills + funding events from each venue, stores them in
`trade_fills`, and reconstructs closed `trade_positions` rows so the PnL
tab shows externally-traded positions even when our reconcile worker
wasn't running at open/close time.

Triggered on demand from the PnL tab via POST /api/trade/pnl/sync. A
Redis lock per user prevents concurrent syncs across replicas. The first
sync per (wallet × exchange × market) pulls ~7 days; subsequent syncs
are deltas via a `last_ts` cursor in `fills_sync_cursor`.

Reconstruction is deterministic: for each (wallet, exchange, market,
symbol) we walk all fills chronologically, maintain a net-quantity +
VWAP entry, and emit a `trade_positions` row when net hits 0.

Idempotency: a row is only emitted if no existing trade_positions row
matches (wallet_id, exchange, symbol, leg_a_market, side) within ±2min
of the candidate's opened_at and closed_at. Re-syncs are no-ops.

Auto-pair detection lives in `trade_service.list_user_pnl` —
reconstructed singles are paired up on read, not on write, so the
existing pair-decision UX (Sync ⇆ / Unpair) keeps working unchanged.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.crypto import decrypt_credentials
from backend.db.models import (
    FillsSyncCursor,
    TradeFill,
    TradePosition,
    Wallet,
)
from backend.db.base import SessionLocal
from backend.services.trade_adapters import ADAPTERS

logger = logging.getLogger("avalant.fills_backfill")

# Backfill window — fixed at 7 days for the first release. Will become a
# user-tunable setting later.
BACKFILL_DAYS = 7

# Per-venue concurrency limit. Each call hits an exchange REST endpoint;
# 4-at-a-time is gentle on rate limits while still finishing a fresh
# user's 7-day pull in seconds.
_VENUE_CONCURRENCY = 4

# A wallet × exchange × market pair gets this many seconds to complete
# before we bail out for the run. 60s lets per-symbol-sweep adapters
# (Binance / Aster / BingX) finish a 7-day window with 50+ symbols.
_PER_VENUE_TIMEOUT_S = 60.0

# Markets every adapter is offered. Adapters that don't support a market
# return [] from fetch_recent_fills; that's a no-op for us.
_MARKETS = ("futures", "spot")


# ── Public API ───────────────────────────────────────────────────────


async def sync_user(user_id: int) -> dict:
    """Run one sync pass for `user_id`. Idempotent across retries.

    Returns a summary dict with counters. Caller is responsible for
    Redis locking; this function does NOT lock itself."""
    db = SessionLocal()
    try:
        wallets = (
            db.query(Wallet)
            .filter(
                Wallet.user_id == user_id,
                Wallet.wallet_type.in_(("exchange", "perpdex")),
                Wallet.purpose.in_(("screener", "both")),
                Wallet.is_archived == False,  # noqa: E712
            )
            .all()
        )
    finally:
        db.close()

    if not wallets:
        return {"wallets": 0, "fills_inserted": 0, "positions_emitted": 0}

    # Build (wallet, market) tasks. Skip MEXC entirely (capability gap
    # documented in CLAUDE.md).
    tasks: list[tuple[Wallet, str]] = []
    for w in wallets:
        ex = (w.type_value or "").lower().strip()
        if ex == "mexc":
            continue
        if ex not in ADAPTERS:
            continue
        adapter = ADAPTERS[ex]
        if not hasattr(adapter, "fetch_recent_fills"):
            continue
        for market in _MARKETS:
            tasks.append((w, market))

    sem = asyncio.Semaphore(_VENUE_CONCURRENCY)
    total_fills = 0
    failed: list[tuple[int, str, str, str]] = []

    async def _one(wallet: Wallet, market: str) -> int:
        async with sem:
            try:
                return await asyncio.wait_for(
                    _sync_one(user_id, wallet, market),
                    timeout=_PER_VENUE_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                failed.append((wallet.id, wallet.type_value, market, "timeout"))
                return 0
            except Exception as exc:  # noqa: BLE001
                failed.append((wallet.id, wallet.type_value, market, repr(exc)[:120]))
                logger.info(
                    "fills_backfill: %s/%s wallet=%s failed: %s",
                    wallet.type_value, market, wallet.id, exc,
                )
                return 0

    counts = await asyncio.gather(*(_one(w, m) for (w, m) in tasks))
    total_fills = sum(counts)

    # Reconstruction: walk all (wallet, exchange, market, symbol) groups
    # under one Session and emit positions. Idempotent re-runs are safe.
    db = SessionLocal()
    try:
        positions_emitted = _reconstruct_positions(db, user_id)
    finally:
        db.close()

    return {
        "wallets": len(wallets),
        "tasks": len(tasks),
        "fills_inserted": total_fills,
        "positions_emitted": positions_emitted,
        "failed": failed,
    }


# ── Per-venue sync ───────────────────────────────────────────────────


async def _sync_one(user_id: int, wallet: Wallet, market: str) -> int:
    """Pull fills for one (wallet, market). Returns number of NEW rows
    written (after de-duplication)."""
    ex = (wallet.type_value or "").lower().strip()
    adapter = ADAPTERS[ex]

    # Read cursor or default to NOW - 7d.
    db = SessionLocal()
    try:
        cur = (
            db.query(FillsSyncCursor)
            .filter(
                FillsSyncCursor.user_id == user_id,
                FillsSyncCursor.wallet_id == wallet.id,
                FillsSyncCursor.exchange == ex,
                FillsSyncCursor.market == market,
            )
            .one_or_none()
        )
        since_ts: datetime
        if cur and cur.last_ts:
            # 1s buffer so we don't miss fills that share a timestamp with
            # the cursor (some venues use second-precision).
            since_ts = cur.last_ts - timedelta(seconds=1)
        else:
            since_ts = datetime.utcnow() - timedelta(days=BACKFILL_DAYS)

        try:
            creds = decrypt_credentials(wallet.credentials or {})
        except Exception as exc:  # noqa: BLE001
            logger.info("fills_backfill: decrypt creds failed for wallet=%s: %s",
                        wallet.id, exc)
            return 0
    finally:
        db.close()

    try:
        rows = await adapter.fetch_recent_fills(creds, since_ts, market=market)
    except NotImplementedError:
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.info("fills_backfill: %s/%s wallet=%s adapter raised: %s",
                    ex, market, wallet.id, exc)
        return 0

    if not rows:
        # Empty return — could be "no fills in window" or "silent API
        # failure". Log so a recurring 0 from an active venue is visible.
        logger.info("fills_backfill: %s/%s wallet=%s returned 0 fills since %s",
                    ex, market, wallet.id, since_ts.isoformat())
        # Touch cursor so we don't re-fetch the same window every time.
        _touch_cursor(user_id, wallet.id, ex, market, since_ts)
        return 0

    inserted = 0
    max_ts: datetime | None = None
    db = SessionLocal()
    try:
        for r in rows:
            try:
                ts = r.get("ts")
                if isinstance(ts, (int, float)):
                    ts = datetime.utcfromtimestamp(ts)
                if not isinstance(ts, datetime):
                    continue
                ext_id = str(r.get("ext_trade_id") or "")
                if not ext_id:
                    continue
                f = TradeFill(
                    user_id=user_id,
                    wallet_id=wallet.id,
                    exchange=ex,
                    market=market,
                    kind=str(r.get("kind") or "trade"),
                    symbol=str(r.get("symbol") or "").upper(),
                    side=(r.get("side") or None),
                    qty=float(r.get("qty") or 0),
                    price=float(r.get("price") or 0),
                    fee_usd=(float(r["fee_usd"]) if r.get("fee_usd") is not None else None),
                    realized_pnl_usd=(float(r["realized_pnl_usd"])
                                      if r.get("realized_pnl_usd") is not None else None),
                    ts=ts,
                    ext_trade_id=ext_id,
                    ext_order_id=(str(r["ext_order_id"]) if r.get("ext_order_id") else None),
                )
                # Use SAVEPOINT (begin_nested) so an IntegrityError on a
                # single duplicate row rolls back ONLY that row, not the
                # whole batch. Plain db.rollback() on the outer session
                # would discard every previously-flushed row in this loop
                # — that's how a single duplicate dropped 12 OKX fills
                # silently and made fills_inserted unreliable.
                try:
                    with db.begin_nested():
                        db.add(f)
                        db.flush()
                    inserted += 1
                except IntegrityError:
                    continue
                if max_ts is None or ts > max_ts:
                    max_ts = ts
            except Exception as exc:  # noqa: BLE001
                logger.info("fills_backfill: row insert failed: %s", exc)
                continue

        # Advance the cursor to max(ts) we ingested. If nothing new came
        # in, leave it where it was so we keep re-pulling the same window.
        if max_ts is not None:
            cur = (
                db.query(FillsSyncCursor)
                .filter(
                    FillsSyncCursor.user_id == user_id,
                    FillsSyncCursor.wallet_id == wallet.id,
                    FillsSyncCursor.exchange == ex,
                    FillsSyncCursor.market == market,
                )
                .one_or_none()
            )
            now = datetime.utcnow()
            if cur is None:
                cur = FillsSyncCursor(
                    user_id=user_id,
                    wallet_id=wallet.id,
                    exchange=ex,
                    market=market,
                    last_ts=max_ts,
                    last_synced_at=now,
                )
                db.add(cur)
            else:
                if cur.last_ts is None or max_ts > cur.last_ts:
                    cur.last_ts = max_ts
                cur.last_synced_at = now
        db.commit()
    finally:
        db.close()

    if inserted:
        logger.info("fills_backfill: ingested wallet=%s ex=%s market=%s rows=%d",
                    wallet.id, ex, market, inserted)
    return inserted


def _touch_cursor(user_id: int, wallet_id: int, exchange: str, market: str,
                  since_ts: datetime) -> None:
    """Mark the cursor as synced even when no new fills arrived. Sets
    last_synced_at and (if cursor missing) last_ts so the UI's
    last_synced_at stays accurate."""
    db = SessionLocal()
    try:
        cur = (
            db.query(FillsSyncCursor)
            .filter(
                FillsSyncCursor.user_id == user_id,
                FillsSyncCursor.wallet_id == wallet_id,
                FillsSyncCursor.exchange == exchange,
                FillsSyncCursor.market == market,
            )
            .one_or_none()
        )
        now = datetime.utcnow()
        if cur is None:
            cur = FillsSyncCursor(
                user_id=user_id, wallet_id=wallet_id, exchange=exchange,
                market=market, last_ts=since_ts, last_synced_at=now,
            )
            db.add(cur)
        else:
            cur.last_synced_at = now
        db.commit()
    finally:
        db.close()


# ── Reconstruction ───────────────────────────────────────────────────


def _reconstruct_positions(db: Session, user_id: int) -> int:
    """Walk all fills for user, group by (wallet, exchange, market, symbol),
    emit closed trade_positions rows where missing.

    Returns count of new rows emitted."""
    cutoff = datetime.utcnow() - timedelta(days=BACKFILL_DAYS + 1)
    fills = (
        db.query(TradeFill)
        .filter(
            TradeFill.user_id == user_id,
            TradeFill.ts >= cutoff,
        )
        .order_by(TradeFill.ts.asc())
        .all()
    )

    grouped: dict[tuple[int | None, str, str, str], list[TradeFill]] = defaultdict(list)
    for f in fills:
        key = (f.wallet_id, f.exchange, f.market, f.symbol)
        grouped[key].append(f)

    emitted = 0
    for (wallet_id, exchange, market, symbol), group in grouped.items():
        emitted += _reconstruct_one(
            db, user_id, wallet_id, exchange, market, symbol, group,
        )

    if emitted:
        db.commit()
    return emitted


def _reconstruct_one(
    db: Session, user_id: int, wallet_id: int | None,
    exchange: str, market: str, symbol: str,
    fills: list[TradeFill],
) -> int:
    """Reconstruct closed positions from a chronological fills stream for
    one (wallet, exchange, market, symbol). Side flips and partial closes
    are handled by tracking signed net qty and emitting whenever net
    crosses zero.

    Funding-kind fills are accumulated into the currently-open position."""
    # State machine
    net_qty: float = 0.0  # signed (positive = long)
    vwap_entry: float = 0.0
    pos_open_ts: datetime | None = None
    pos_realized: float = 0.0
    pos_funding: float = 0.0
    pos_fees: float = 0.0
    pos_open_qty: float = 0.0   # total opened so far this position
    pos_close_qty: float = 0.0  # total closed (for VWAP-exit calc)
    pos_close_value: float = 0.0  # Σ price × closed_qty

    emitted = 0

    def _emit(close_ts: datetime, close_price: float) -> int:
        """Materialize a trade_positions row from the position state. Idempotent:
        skips if a matching row already exists."""
        nonlocal pos_open_ts, pos_realized, pos_funding, pos_fees
        nonlocal pos_open_qty, pos_close_qty, pos_close_value
        if pos_open_ts is None or pos_open_qty <= 0:
            return 0
        side = "buy" if vwap_entry_side > 0 else "sell"
        # Idempotency: check for an existing closed row at the same
        # wallet × exchange × market × side × symbol within ±2 min of
        # opened_at AND closed_at.
        win = timedelta(minutes=2)
        existing = (
            db.query(TradePosition)
            .filter(
                TradePosition.user_id == user_id,
                TradePosition.kind == "single",
                TradePosition.status == "closed",
                TradePosition.symbol == symbol,
                TradePosition.leg_a_exchange == exchange,
                TradePosition.leg_a_market == market,
                TradePosition.leg_a_side == side,
                TradePosition.opened_at >= pos_open_ts - win,
                TradePosition.opened_at <= pos_open_ts + win,
                TradePosition.closed_at >= close_ts - win,
                TradePosition.closed_at <= close_ts + win,
            )
            .first()
        )
        if existing is not None:
            return 0

        exit_vwap = (pos_close_value / pos_close_qty) if pos_close_qty > 0 else close_price
        row = TradePosition(
            user_id=user_id,
            kind="single",
            status="closed",
            symbol=symbol,
            leg_a_wallet_id=wallet_id,
            leg_a_exchange=exchange,
            leg_a_side=side,
            leg_a_qty=pos_open_qty,
            leg_a_entry_price=vwap_entry,
            leg_a_exit_price=exit_vwap,
            leg_a_realized_pnl_usd=pos_realized,
            leg_a_funding_pnl_usd=pos_funding if pos_funding else None,
            leg_a_fees_usd=pos_fees,
            leg_a_market=market,
            opened_at=pos_open_ts,
            closed_at=close_ts,
            opened_externally=True,
            closed_externally=True,
            source="fills_backfill",
            realized_pnl_usd=pos_realized,
        )
        db.add(row)
        return 1

    vwap_entry_side: int = 0  # +1 long, -1 short

    for f in fills:
        if f.kind == "funding":
            if pos_open_ts is not None:
                pos_funding += float(f.realized_pnl_usd or 0)
            continue

        side = (f.side or "").lower()
        if side not in ("buy", "sell"):
            continue
        signed = float(f.qty or 0) * (1.0 if side == "buy" else -1.0)
        if signed == 0:
            continue
        price = float(f.price or 0)
        fee = float(f.fee_usd or 0)
        ts = f.ts

        # Spot can only hold longs in our model. If we somehow see a SELL
        # while net_qty == 0, treat it as opening a short for futures or
        # ignore for spot.
        if net_qty == 0:
            net_qty = signed
            vwap_entry = price
            vwap_entry_side = 1 if signed > 0 else -1
            pos_open_ts = ts
            pos_realized = 0.0
            pos_funding = 0.0
            pos_fees = fee
            pos_open_qty = abs(signed)
            pos_close_qty = 0.0
            pos_close_value = 0.0
            continue

        same_side = (net_qty > 0 and signed > 0) or (net_qty < 0 and signed < 0)
        if same_side:
            # DCA — extend position, recompute VWAP.
            new_size = abs(net_qty) + abs(signed)
            vwap_entry = (vwap_entry * abs(net_qty) + price * abs(signed)) / new_size
            net_qty += signed
            pos_open_qty += abs(signed)
            pos_fees += fee
            continue

        # Opposite side — reduce or flip.
        reducing_qty = min(abs(net_qty), abs(signed))
        sign_factor = 1.0 if vwap_entry_side > 0 else -1.0
        # Use exchange-supplied realized PnL when present; fall back to VWAP-diff.
        if f.realized_pnl_usd is not None:
            pos_realized += float(f.realized_pnl_usd)
        else:
            pos_realized += sign_factor * (price - vwap_entry) * reducing_qty
        pos_close_qty += reducing_qty
        pos_close_value += price * reducing_qty
        pos_fees += fee
        net_qty += signed

        if abs(net_qty) < 1e-9:
            # Fully closed — emit row.
            emitted += _emit(ts, price)
            net_qty = 0.0
            vwap_entry = 0.0
            vwap_entry_side = 0
            pos_open_ts = None
        elif (vwap_entry_side > 0 and net_qty < 0) or (vwap_entry_side < 0 and net_qty > 0):
            # Flipped through zero — close current, open new.
            emitted += _emit(ts, price)
            # Remaining qty becomes the new opener at the same price.
            net_qty = net_qty  # already correct
            vwap_entry = price
            vwap_entry_side = 1 if net_qty > 0 else -1
            pos_open_ts = ts
            pos_realized = 0.0
            pos_funding = 0.0
            pos_fees = 0.0
            pos_open_qty = abs(net_qty)
            pos_close_qty = 0.0
            pos_close_value = 0.0

    return emitted
