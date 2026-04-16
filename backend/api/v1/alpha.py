"""Alpha features — paper trading, slippage, health, replay, correlation, anomaly, backtest, watchlist."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from backend.api.deps import get_current_user, get_db
from backend.db.models import User, WatchlistItem

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,16}$")
_KNOWN_EX = {
    "binance", "bybit", "okx", "gate", "kucoin", "mexc", "bitget",
    "hyperliquid", "aster", "ethereal", "whitebit", "bingx", "lighter", "paradex",
}


def _norm_symbol(v):
    s = str(v or "").strip().upper()
    if not _SYMBOL_RE.match(s):
        raise ValueError("symbol must be 1-16 alphanumeric uppercase")
    return s


def _norm_exchange(v):
    s = str(v or "").strip().lower()
    if s not in _KNOWN_EX:
        raise ValueError(f"unknown exchange: {v!r}")
    return s
from backend.services import (
    alpha_service,
    anomaly_service,
    backtest_service,
    correlation_service,
    health_service,
    paper_service,
    replay_service,
    slippage_service,
)
from backend.services.arbitrage_service import get_arbitrage_opportunities

router = APIRouter(prefix="/screener", tags=["screener-alpha"])


# ── Alpha Score ───────────────────────────────────────────────────────────────
@router.get("/alpha")
async def alpha_ranked(_=Depends(get_current_user)):
    """Arbitrage opportunities annotated with alpha_score (0-100) and alpha_rank."""
    data = await get_arbitrage_opportunities()
    opps = list(data.get("opportunities", []))
    alpha_service.score_opportunities(opps)
    return {"opportunities": opps, "fees": data.get("fees", {})}


# ── Paper Trading ─────────────────────────────────────────────────────────────
class OpenPositionIn(BaseModel):
    symbol: str
    long_exchange: str
    short_exchange: str
    size_usd: float = Field(..., gt=10, le=1_000_000)

    @field_validator("symbol", mode="before")
    @classmethod
    def _sym(cls, v): return _norm_symbol(v)

    @field_validator("long_exchange", "short_exchange", mode="before")
    @classmethod
    def _ex(cls, v): return _norm_exchange(v)


@router.get("/paper/positions")
async def paper_list(
    status: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await paper_service.list_positions(db, user.id, status=status)


@router.get("/paper/stats")
async def paper_stats(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return await paper_service.stats(db, user.id)


@router.post("/paper/positions")
async def paper_open(
    body: OpenPositionIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.long_exchange == body.short_exchange:
        raise HTTPException(400, "long_exchange and short_exchange must differ")
    try:
        return await paper_service.open_position(
            db, user.id, body.symbol, body.long_exchange, body.short_exchange, body.size_usd
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/paper/positions/{pid}/close")
async def paper_close(
    pid: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    try:
        return await paper_service.close_position(db, user.id, pid)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.delete("/paper/positions/{pid}")
def paper_delete(pid: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        paper_service.delete_position(db, user.id, pid)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(404, str(e))


# ── Executable Spread / Slippage ──────────────────────────────────────────────
@router.get("/executable-spread")
async def executable_spread(
    symbol: str,
    long_ex: str,
    short_ex: str,
    size_usd: float = Query(1000, gt=10, le=5_000_000),
    _=Depends(get_current_user),
):
    return await slippage_service.calculate(symbol, long_ex, short_ex, size_usd)


# ── Exchange Health ───────────────────────────────────────────────────────────
@router.get("/health")
def exchange_health(
    window_min: int = Query(60, ge=5, le=1440),
    _=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return health_service.summary(db, window_min=window_min)


# ── Spread Replay ─────────────────────────────────────────────────────────────
@router.get("/replay")
def replay_history(
    symbol: str,
    long_ex: str,
    short_ex: str,
    hours: int = Query(24, ge=1, le=168),
    _=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return replay_service.pair_history(db, symbol, long_ex, short_ex, hours=hours)


@router.get("/leaderboard")
def leaderboard(
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(20, ge=5, le=100),
    _=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return replay_service.leaderboard(db, hours=hours, limit=limit)


# ── Correlation Matrix ────────────────────────────────────────────────────────
@router.get("/correlation")
def correlation(
    hours: int = Query(24, ge=1, le=168),
    top_n: int = Query(15, ge=5, le=30),
    _=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return correlation_service.matrix(db, hours=hours, top_n=top_n)


# ── Anomaly Events ────────────────────────────────────────────────────────────
@router.get("/anomalies")
def anomalies(
    hours: int = Query(24, ge=1, le=72),
    limit: int = Query(50, ge=5, le=200),
    _=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return anomaly_service.recent(db, hours=hours, limit=limit)


# ── Quick Backtest ────────────────────────────────────────────────────────────
@router.get("/backtest")
async def backtest_endpoint(
    symbol: str,
    long_ex: str,
    short_ex: str,
    size_usd: float = Query(1000, gt=10, le=5_000_000),
    days: int = Query(7, ge=1, le=30),
    _=Depends(get_current_user),
):
    return await backtest_service.backtest(symbol, long_ex, short_ex, size_usd, days=days)


# ── Watchlist ─────────────────────────────────────────────────────────────────
class WatchlistIn(BaseModel):
    symbol: str
    long_exchange: str
    short_exchange: str
    note: str | None = Field(None, max_length=200)

    @field_validator("symbol", mode="before")
    @classmethod
    def _sym(cls, v): return _norm_symbol(v)

    @field_validator("long_exchange", "short_exchange", mode="before")
    @classmethod
    def _ex(cls, v): return _norm_exchange(v)


@router.get("/watchlist")
def watchlist_get(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(WatchlistItem).filter(WatchlistItem.user_id == user.id).order_by(WatchlistItem.created_at.desc()).all()
    return [{
        "id": r.id, "symbol": r.symbol, "long_exchange": r.long_exchange,
        "short_exchange": r.short_exchange, "note": r.note,
        "created_at": r.created_at.isoformat(),
    } for r in rows]


@router.post("/watchlist")
def watchlist_add(
    body: WatchlistIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.long_exchange == body.short_exchange:
        raise HTTPException(400, "long_exchange and short_exchange must differ")
    # Dedup — no DB unique constraint today, enforce at service level
    dup = (
        db.query(WatchlistItem)
        .filter(
            WatchlistItem.user_id == user.id,
            WatchlistItem.symbol == body.symbol,
            WatchlistItem.long_exchange == body.long_exchange,
            WatchlistItem.short_exchange == body.short_exchange,
        )
        .first()
    )
    if dup:
        return {"id": dup.id, "duplicate": True}
    item = WatchlistItem(
        user_id=user.id,
        symbol=body.symbol,
        long_exchange=body.long_exchange,
        short_exchange=body.short_exchange,
        note=body.note,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id}


@router.delete("/watchlist/{item_id}")
def watchlist_delete(
    item_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.query(WatchlistItem).filter(WatchlistItem.id == item_id, WatchlistItem.user_id == user.id).first()
    if not item:
        raise HTTPException(404, "Not found")
    db.delete(item)
    db.commit()
    return {"ok": True}
