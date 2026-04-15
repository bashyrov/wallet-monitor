"""Anomaly Detector — z-score outlier detection on recent spread history per pair.

For each pair, compute μ/σ of `net_profit` over last 6h, flag current values where
|z| > threshold. Emits AnomalyEvent rows; de-duped with 15min cooldown per pair.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.db.base import SessionLocal
from backend.db.models import AnomalyEvent, OpportunitySnapshot
from backend.services.arbitrage_service import get_arbitrage_opportunities

logger = logging.getLogger("avalant.anomaly")

Z_THRESHOLD = 3.0
COOLDOWN_MIN = 15
WINDOW_H = 6


def _stats(values: list[float]) -> tuple[float, float]:
    n = len(values)
    if n < 5:
        return 0.0, 0.0
    m = sum(values) / n
    var = sum((v - m) ** 2 for v in values) / n
    return m, math.sqrt(var)


async def scan_once() -> list[dict]:
    data = await get_arbitrage_opportunities()
    current = list(data.get("opportunities", []))
    if not current:
        return []

    cutoff = datetime.utcnow() - timedelta(hours=WINDOW_H)
    cooldown = datetime.utcnow() - timedelta(minutes=COOLDOWN_MIN)
    events: list[dict] = []

    db: Session = SessionLocal()
    try:
        # group historical by pair
        rows = (
            db.query(OpportunitySnapshot)
            .filter(OpportunitySnapshot.snapshot_at >= cutoff)
            .all()
        )
        hist: dict[tuple[str, str, str], list[float]] = {}
        for r in rows:
            hist.setdefault((r.symbol, r.long_exchange, r.short_exchange), []).append(r.net_profit)

        for o in current:
            key = (o.get("symbol"), o.get("long_exchange"), o.get("short_exchange"))
            series = hist.get(key, [])
            if len(series) < 20:
                continue
            mean, std = _stats(series)
            if std < 1e-8:
                continue
            cur = float(o.get("net_profit", 0) or 0)
            z = (cur - mean) / std
            if abs(z) < Z_THRESHOLD:
                continue

            # cooldown check
            existing = (
                db.query(AnomalyEvent)
                .filter(
                    AnomalyEvent.symbol == key[0],
                    AnomalyEvent.long_exchange == key[1],
                    AnomalyEvent.short_exchange == key[2],
                    AnomalyEvent.created_at >= cooldown,
                )
                .first()
            )
            if existing:
                continue

            ev = AnomalyEvent(
                symbol=key[0], long_exchange=key[1], short_exchange=key[2],
                spread_pct=cur, z_score=round(z, 2),
                mean_pct=round(mean, 4), std_pct=round(std, 4),
            )
            db.add(ev)
            events.append({
                "symbol": key[0], "long_exchange": key[1], "short_exchange": key[2],
                "spread_pct": cur, "z_score": round(z, 2),
                "mean_pct": round(mean, 4), "std_pct": round(std, 4),
            })

        # prune >48h
        oldcut = datetime.utcnow() - timedelta(hours=48)
        db.query(AnomalyEvent).filter(AnomalyEvent.created_at < oldcut).delete()
        db.commit()
    finally:
        db.close()
    return events


def recent(db: Session, hours: int = 24, limit: int = 50) -> list[dict]:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    rows = (
        db.query(AnomalyEvent)
        .filter(AnomalyEvent.created_at >= cutoff)
        .order_by(AnomalyEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    return [{
        "id": r.id,
        "symbol": r.symbol,
        "long_exchange": r.long_exchange,
        "short_exchange": r.short_exchange,
        "spread_pct": round(r.spread_pct, 4),
        "z_score": round(r.z_score, 2),
        "mean_pct": round(r.mean_pct, 4),
        "std_pct": round(r.std_pct, 4),
        "created_at": r.created_at.isoformat(),
    } for r in rows]


async def anomaly_loop(interval_s: int = 120):
    logger.info("Anomaly detector started (interval=%ss, z=%s)", interval_s, Z_THRESHOLD)
    while True:
        try:
            events = await scan_once()
            if events:
                logger.info("Detected %d spread anomalies", len(events))
        except Exception as e:
            logger.error("Anomaly scan failed: %s", e)
        await asyncio.sleep(interval_s)
