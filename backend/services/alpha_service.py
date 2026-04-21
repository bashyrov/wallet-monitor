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

import bisect
import math
from typing import Iterable


def _rank_pct_bisect(sorted_vals: list[float], v: float, n: int) -> float:
    """Percentile rank of v within pre-sorted values (0-1). O(log N) via bisect.
    Ties split half-and-half like the original implementation."""
    if n == 0:
        return 0.5
    lo = bisect.bisect_left(sorted_vals, v)
    hi = bisect.bisect_right(sorted_vals, v)
    # lo = count strictly below; hi - lo = count equal
    return (lo + 0.5 * (hi - lo)) / n


def score_opportunities(opps: list[dict]) -> list[dict]:
    """Annotate each opp with `alpha_score` (0-100) and `alpha_rank`.

    Performance: the naive O(N²) version took 5-7s on the prod fetcher
    with ~3300 opps per tick and was the dominant reason arbitrage.json
    was being refreshed every 6-9s instead of the configured 600ms.
    Pre-sorting each metric once then using bisect.bisect_* brings this
    to O(N log N) ≈ ~100ms for the same input.
    """
    if not opps:
        return opps

    n = len(opps)
    spreads_all = [float(o.get("net_profit", 0) or 0) for o in opps]
    volumes_all = [math.log1p(max(float(o.get("long_volume", 0) or 0),
                                  float(o.get("short_volume", 0) or 0))) for o in opps]
    intervals_all = [float(o.get("long_interval_h", 8) or 8)
                     + float(o.get("short_interval_h", 8) or 8) for o in opps]
    inv_price_all = [-abs(float(o.get("price_spread", 0) or 0)) for o in opps]

    spreads_sorted   = sorted(spreads_all)
    volumes_sorted   = sorted(volumes_all)
    intervals_sorted = sorted(intervals_all)
    inv_price_sorted = sorted(inv_price_all)

    for o, s, v, it, ip in zip(opps, spreads_all, volumes_all, intervals_all, inv_price_all):
        p_spread = _rank_pct_bisect(spreads_sorted, s, n)
        p_vol    = _rank_pct_bisect(volumes_sorted, v, n)
        p_ival   = 1 - _rank_pct_bisect(intervals_sorted, it, n)
        p_price  = _rank_pct_bisect(inv_price_sorted, ip, n)
        score = 100 * (0.45 * p_spread + 0.25 * p_vol + 0.15 * p_ival + 0.15 * p_price)
        o["alpha_score"] = round(score, 1)

    opps.sort(key=lambda o: o.get("alpha_score", 0), reverse=True)
    for i, o in enumerate(opps, 1):
        o["alpha_rank"] = i
    return opps
