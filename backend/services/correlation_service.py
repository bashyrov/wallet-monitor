"""Correlation Matrix — which symbols' arb spreads move together."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.db.models import OpportunitySnapshot


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def matrix(db: Session, hours: int = 24, top_n: int = 15) -> dict:
    """Build a correlation matrix for the top-N pairs (by sample count)."""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    rows = (
        db.query(OpportunitySnapshot)
        .filter(OpportunitySnapshot.snapshot_at >= cutoff)
        .order_by(OpportunitySnapshot.snapshot_at.asc())
        .all()
    )

    series: dict[str, list[tuple[datetime, float]]] = {}
    for r in rows:
        pair = f"{r.symbol}|{r.long_exchange}>{r.short_exchange}"
        series.setdefault(pair, []).append((r.snapshot_at, r.net_profit))

    # Keep only pairs with enough samples
    series = {k: v for k, v in series.items() if len(v) >= 10}

    # Rank by sample count
    top_pairs = sorted(series.keys(), key=lambda k: len(series[k]), reverse=True)[:top_n]

    # Align on 5-minute buckets
    def bucketize(points: list[tuple[datetime, float]]) -> dict[int, float]:
        buckets: dict[int, list[float]] = {}
        for ts, v in points:
            bucket = int(ts.timestamp() // 300) * 300
            buckets.setdefault(bucket, []).append(v)
        return {b: sum(vs) / len(vs) for b, vs in buckets.items()}

    bucketed = {k: bucketize(series[k]) for k in top_pairs}

    # Common buckets for all pairs
    common = None
    for v in bucketed.values():
        if common is None:
            common = set(v.keys())
        else:
            common &= set(v.keys())
    common_sorted = sorted(common or [])

    if len(common_sorted) < 3:
        return {"pairs": top_pairs, "matrix": [], "samples": 0}

    matrix_rows = []
    for a in top_pairs:
        row = []
        xs = [bucketed[a][b] for b in common_sorted]
        for b in top_pairs:
            if a == b:
                row.append(1.0)
            else:
                ys = [bucketed[b][bk] for bk in common_sorted]
                row.append(round(_pearson(xs, ys), 3))
        matrix_rows.append(row)

    return {"pairs": top_pairs, "matrix": matrix_rows, "samples": len(common_sorted)}
