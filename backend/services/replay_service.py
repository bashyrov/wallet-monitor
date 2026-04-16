"""Spread Replay — periodically snapshot current opportunities into DB,
then expose scrubber endpoint for 24h historical playback."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.db.base import SessionLocal
from backend.db.models import OpportunitySnapshot
from backend.services.alpha_service import score_opportunities
from backend.services.arbitrage_service import get_arbitrage_opportunities

logger = logging.getLogger("avalant.replay")


async def snapshot_current() -> int:
    """Write one row per current opportunity."""
    data = await get_arbitrage_opportunities()
    opps = list(data.get("opportunities", []))
    score_opportunities(opps)
    now = datetime.utcnow()
    db: Session = SessionLocal()
    try:
        # keep the top 200 opportunities by alpha to cap storage
        opps.sort(key=lambda o: o.get("alpha_score", 0), reverse=True)
        for o in opps[:200]:
            db.add(OpportunitySnapshot(
                symbol=o.get("symbol", ""),
                long_exchange=o.get("long_exchange", ""),
                short_exchange=o.get("short_exchange", ""),
                gross_funding=float(o.get("gross_funding", 0) or 0),
                price_spread=float(o.get("price_spread", 0) or 0),
                net_profit=float(o.get("net_profit", 0) or 0),
                long_rate=float(o.get("long_rate", 0) or 0),
                short_rate=float(o.get("short_rate", 0) or 0),
                long_volume=float(o.get("long_volume", 0) or 0),
                short_volume=float(o.get("short_volume", 0) or 0),
                alpha_score=float(o.get("alpha_score", 0) or 0),
                snapshot_at=now,
            ))
        # prune older than 7 days
        cutoff = datetime.utcnow() - timedelta(days=7)
        db.query(OpportunitySnapshot).filter(OpportunitySnapshot.snapshot_at < cutoff).delete()
        db.commit()
        return len(opps[:200])
    finally:
        db.close()


def pair_history(db: Session, symbol: str, long_ex: str, short_ex: str, hours: int = 24) -> list[dict]:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    rows = (
        db.query(OpportunitySnapshot)
        .filter(
            OpportunitySnapshot.symbol == symbol.upper(),
            OpportunitySnapshot.long_exchange == long_ex,
            OpportunitySnapshot.short_exchange == short_ex,
            OpportunitySnapshot.snapshot_at >= cutoff,
        )
        .order_by(OpportunitySnapshot.snapshot_at.asc())
        .all()
    )
    return [{
        "ts": int(r.snapshot_at.timestamp()),
        "net": round(r.net_profit, 4),
        "gross": round(r.gross_funding, 4),
        "spread": round(r.price_spread, 4),
        "alpha": round(r.alpha_score or 0, 1),
        "long_rate": round(r.long_rate, 4),
        "short_rate": round(r.short_rate, 4),
        "vol_l": round(r.long_volume, 2),
        "vol_s": round(r.short_volume, 2),
    } for r in rows]


def leaderboard(db: Session, hours: int = 24, limit: int = 20) -> list[dict]:
    """Top pairs by average alpha score over the window."""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    rows = (
        db.query(OpportunitySnapshot)
        .filter(OpportunitySnapshot.snapshot_at >= cutoff)
        .all()
    )
    agg: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        key = (r.symbol, r.long_exchange, r.short_exchange)
        if key not in agg:
            agg[key] = {
                "symbol": r.symbol, "long_exchange": r.long_exchange, "short_exchange": r.short_exchange,
                "n": 0, "alpha_sum": 0.0, "net_sum": 0.0, "net_max": float("-inf"), "net_min": float("inf"),
            }
        a = agg[key]
        a["n"] += 1
        a["alpha_sum"] += r.alpha_score or 0
        a["net_sum"] += r.net_profit
        a["net_max"] = max(a["net_max"], r.net_profit)
        a["net_min"] = min(a["net_min"], r.net_profit)

    out = []
    for k, a in agg.items():
        if a["n"] < 3:
            continue
        out.append({
            "symbol": a["symbol"],
            "long_exchange": a["long_exchange"],
            "short_exchange": a["short_exchange"],
            "avg_alpha": round(a["alpha_sum"] / a["n"], 1),
            "avg_net": round(a["net_sum"] / a["n"], 4),
            "max_net": round(a["net_max"], 4),
            "min_net": round(a["net_min"], 4),
            "samples": a["n"],
        })
    out.sort(key=lambda x: x["avg_alpha"], reverse=True)
    return out[:limit]


async def snapshot_loop(interval_s: int = 60):
    logger.info("Opportunity snapshot loop started (interval=%ss)", interval_s)
    while True:
        try:
            n = await snapshot_current()
            logger.debug("Snapshotted %d opportunities", n)
        except Exception as e:
            logger.error("Snapshot failed: %s", e)
        await asyncio.sleep(interval_s)
