"""Paper Trading — simulate arb positions with live P&L tracking.

Position P&L model (per-dollar notional, assuming 1x leverage on each leg):

  notional_tokens_long  = size_usd / entry_long_price
  notional_tokens_short = size_usd / entry_short_price

  Spot leg P&L:
    long_pnl  = notional_tokens_long  * (cur_long_price - entry_long_price)
    short_pnl = notional_tokens_short * (entry_short_price - cur_short_price)
    price_pnl = long_pnl + short_pnl

  Funding P&L (accrued over time):
    short pays/receives funding each interval based on short_rate
    long  receives/pays funding each interval based on long_rate
    (positive funding rate → longs pay shorts)
    funding_pnl ≈ size_usd * (short_rate - long_rate) * (8h intervals elapsed / 8)

  Net unrealized P&L = price_pnl + funding_pnl - entry_fees_usd

All expressed in USD. `size_usd` is the notional per leg; total margin ~ 2x size_usd.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from backend.db.models import PaperPosition
from backend.services.arbitrage_service import get_cached_rates, EXCHANGE_FEES


def _current_prices_and_rates() -> dict[tuple[str, str], dict]:
    """Build lookup of current price/rate per (symbol, exchange)."""
    flat = get_cached_rates()  # {"exchange:symbol" → {rate, interval_h, price}}
    out: dict[tuple[str, str], dict] = {}
    for key, row in flat.items():
        if ":" not in key:
            continue
        ex, sym = key.split(":", 1)
        out[(sym, ex)] = row
    return out


def _compute_live(position: PaperPosition, book: dict[tuple[str, str], dict]) -> dict[str, Any]:
    long_row = book.get((position.symbol, position.long_exchange))
    short_row = book.get((position.symbol, position.short_exchange))

    cur_long = float(long_row.get("price") or 0) if long_row else position.entry_long_price
    cur_short = float(short_row.get("price") or 0) if short_row else position.entry_short_price

    # Convert rates (8h-normalized %) into a per-8h accrual
    long_rate = float(long_row.get("rate") or 0) if long_row else 0.0
    short_rate = float(short_row.get("rate") or 0) if short_row else 0.0

    tokens_long = position.size_usd / position.entry_long_price if position.entry_long_price else 0
    tokens_short = position.size_usd / position.entry_short_price if position.entry_short_price else 0

    long_pnl = tokens_long * (cur_long - position.entry_long_price)
    short_pnl = tokens_short * (position.entry_short_price - cur_short)
    price_pnl = long_pnl + short_pnl

    elapsed_s = (datetime.utcnow() - position.opened_at).total_seconds()
    intervals_8h = elapsed_s / (8 * 3600)
    funding_accrual = position.size_usd * ((short_rate - long_rate) / 100.0) * intervals_8h

    total_accrued = position.accrued_funding_usd + funding_accrual
    gross = price_pnl + total_accrued
    net = gross - position.entry_fees_usd

    cur_spread_pct = ((cur_short - cur_long) / cur_long * 100) if cur_long else 0

    return {
        "id": position.id,
        "symbol": position.symbol,
        "long_exchange": position.long_exchange,
        "short_exchange": position.short_exchange,
        "size_usd": position.size_usd,
        "status": position.status,
        "entry_long_price": position.entry_long_price,
        "entry_short_price": position.entry_short_price,
        "entry_spread_pct": position.entry_spread_pct,
        "entry_fees_usd": position.entry_fees_usd,
        "current_long_price": cur_long,
        "current_short_price": cur_short,
        "current_spread_pct": round(cur_spread_pct, 4),
        "price_pnl_usd": round(price_pnl, 4),
        "funding_pnl_usd": round(total_accrued, 4),
        "gross_pnl_usd": round(gross, 4),
        "net_pnl_usd": round(net, 4),
        "net_pnl_pct": round(net / position.size_usd * 100, 3) if position.size_usd else 0,
        "opened_at": position.opened_at.isoformat(),
        "closed_at": position.closed_at.isoformat() if position.closed_at else None,
        "realized_pnl_usd": position.realized_pnl_usd,
        "exit_spread_pct": position.exit_spread_pct,
    }


async def list_positions(db: Session, user_id: int, status: str | None = None) -> list[dict]:
    q = db.query(PaperPosition).filter(PaperPosition.user_id == user_id)
    if status:
        q = q.filter(PaperPosition.status == status)
    positions = q.order_by(PaperPosition.opened_at.desc()).all()

    open_positions = [p for p in positions if p.status == "open"]
    book = _current_prices_and_rates() if open_positions else {}

    out: list[dict] = []
    for p in positions:
        if p.status == "open":
            out.append(_compute_live(p, book))
        else:
            out.append({
                "id": p.id,
                "symbol": p.symbol,
                "long_exchange": p.long_exchange,
                "short_exchange": p.short_exchange,
                "size_usd": p.size_usd,
                "status": p.status,
                "entry_spread_pct": p.entry_spread_pct,
                "exit_spread_pct": p.exit_spread_pct,
                "realized_pnl_usd": p.realized_pnl_usd,
                "net_pnl_pct": round((p.realized_pnl_usd or 0) / p.size_usd * 100, 3) if p.size_usd else 0,
                "opened_at": p.opened_at.isoformat(),
                "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            })
    return out


async def open_position(
    db: Session, user_id: int, symbol: str, long_ex: str, short_ex: str, size_usd: float
) -> dict:
    book = _current_prices_and_rates()
    long_row = book.get((symbol, long_ex))
    short_row = book.get((symbol, short_ex))
    if not long_row or not short_row:
        raise ValueError(f"No live quotes for {symbol} on {long_ex}/{short_ex}")

    p_long = float(long_row.get("price") or 0)
    p_short = float(short_row.get("price") or 0)
    if p_long <= 0 or p_short <= 0:
        raise ValueError("Invalid prices")

    spread_pct = (p_short - p_long) / p_long * 100
    fee_rate = (EXCHANGE_FEES.get(long_ex, 0.0005) + EXCHANGE_FEES.get(short_ex, 0.0005))
    fees = size_usd * fee_rate * 2  # entry both legs

    pos = PaperPosition(
        user_id=user_id,
        symbol=symbol.upper(),
        long_exchange=long_ex,
        short_exchange=short_ex,
        size_usd=size_usd,
        entry_long_price=p_long,
        entry_short_price=p_short,
        entry_spread_pct=round(spread_pct, 4),
        entry_fees_usd=round(fees, 4),
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return _compute_live(pos, book)


async def close_position(db: Session, user_id: int, position_id: int) -> dict:
    pos = (
        db.query(PaperPosition)
        .filter(PaperPosition.id == position_id, PaperPosition.user_id == user_id)
        .first()
    )
    if not pos:
        raise ValueError("Position not found")
    if pos.status != "open":
        raise ValueError("Position already closed")

    book = _current_prices_and_rates()
    live = _compute_live(pos, book)

    exit_fee_rate = (EXCHANGE_FEES.get(pos.long_exchange, 0.0005) + EXCHANGE_FEES.get(pos.short_exchange, 0.0005))
    exit_fees = pos.size_usd * exit_fee_rate * 2
    realized = live["net_pnl_usd"] - exit_fees

    pos.status = "closed"
    pos.closed_at = datetime.utcnow()
    pos.exit_spread_pct = live["current_spread_pct"]
    pos.realized_pnl_usd = round(realized, 4)
    pos.accrued_funding_usd = live["funding_pnl_usd"]
    pos.last_updated = datetime.utcnow()
    db.commit()
    db.refresh(pos)
    return {
        **live,
        "status": "closed",
        "realized_pnl_usd": pos.realized_pnl_usd,
        "exit_spread_pct": pos.exit_spread_pct,
        "closed_at": pos.closed_at.isoformat(),
    }


def delete_position(db: Session, user_id: int, position_id: int) -> None:
    pos = (
        db.query(PaperPosition)
        .filter(PaperPosition.id == position_id, PaperPosition.user_id == user_id)
        .first()
    )
    if not pos:
        raise ValueError("Position not found")
    db.delete(pos)
    db.commit()


async def stats(db: Session, user_id: int) -> dict:
    positions = db.query(PaperPosition).filter(PaperPosition.user_id == user_id).all()
    closed = [p for p in positions if p.status == "closed" and p.realized_pnl_usd is not None]
    open_positions = [p for p in positions if p.status == "open"]

    live_pnl = 0.0
    if open_positions:
        book = _current_prices_and_rates()
        for p in open_positions:
            live_pnl += _compute_live(p, book)["net_pnl_usd"]

    realized = sum(p.realized_pnl_usd or 0 for p in closed)
    wins = sum(1 for p in closed if (p.realized_pnl_usd or 0) > 0)
    losses = sum(1 for p in closed if (p.realized_pnl_usd or 0) <= 0)
    win_rate = wins / len(closed) * 100 if closed else 0

    return {
        "total_positions": len(positions),
        "open_count": len(open_positions),
        "closed_count": len(closed),
        "win_count": wins,
        "loss_count": losses,
        "win_rate_pct": round(win_rate, 1),
        "realized_pnl_usd": round(realized, 2),
        "unrealized_pnl_usd": round(live_pnl, 2),
        "total_pnl_usd": round(realized + live_pnl, 2),
    }
