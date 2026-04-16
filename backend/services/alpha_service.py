"""Alpha Score — composite 0-100 rank for arb opportunities.

Combines four normalized factors:
  - Spread magnitude (higher = better)
  - Volume / liquidity (higher = better)
  - Funding interval (shorter = more frequent payouts = better)
  - Price spread inversion (|price_spread| close to 0 = cleaner entry)

The score is a weighted sum of percentile ranks within the current opportunity set,
so it's always interpretable as "top 10% = score 90+".
"""
from __future__ import annotations

import math
from typing import Iterable


def _rank_pct(values: list[float], v: float) -> float:
    """Percentile rank of v within values (0-1)."""
    if not values:
        return 0.5
    below = sum(1 for x in values if x < v)
    equal = sum(1 for x in values if x == v)
    return (below + 0.5 * equal) / len(values)


def score_opportunities(opps: list[dict]) -> list[dict]:
    """Annotate each opp with `alpha_score` (0-100) and `alpha_rank`."""
    if not opps:
        return opps

    spreads = [float(o.get("net_profit", 0) or 0) for o in opps]
    volumes = [math.log1p(max(float(o.get("long_volume", 0) or 0), float(o.get("short_volume", 0) or 0))) for o in opps]
    intervals = [float(o.get("long_interval_h", 8) or 8) + float(o.get("short_interval_h", 8) or 8) for o in opps]
    inv_price = [-abs(float(o.get("price_spread", 0) or 0)) for o in opps]  # closer to 0 = better

    for o in opps:
        s = float(o.get("net_profit", 0) or 0)
        v = math.log1p(max(float(o.get("long_volume", 0) or 0), float(o.get("short_volume", 0) or 0)))
        it = float(o.get("long_interval_h", 8) or 8) + float(o.get("short_interval_h", 8) or 8)
        ip = -abs(float(o.get("price_spread", 0) or 0))

        p_spread = _rank_pct(spreads, s)
        p_vol = _rank_pct(volumes, v)
        p_ival = 1 - _rank_pct(intervals, it)  # shorter interval = higher rank
        p_price = _rank_pct(inv_price, ip)

        score = 100 * (0.45 * p_spread + 0.25 * p_vol + 0.15 * p_ival + 0.15 * p_price)
        o["alpha_score"] = round(score, 1)

    opps.sort(key=lambda o: o.get("alpha_score", 0), reverse=True)
    for i, o in enumerate(opps, 1):
        o["alpha_rank"] = i
    return opps
