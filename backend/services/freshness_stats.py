"""Rolling per-exchange freshness statistics for the admin dashboard.

`get_exchange_health` already reports the *current* age_s per venue;
this module remembers a rolling 5-minute window of those samples so the
admin page can show "average freshness" — the metric users actually
care about (a venue can spike to 30s once and recover; an average tells
us whether it's chronically slow).

State is in-memory per-process. Each replica samples independently;
the admin endpoint aggregates them via the file-cache shim used by
arbitrage_service for cross-process state.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Iterable

# Window length — ages older than this are dropped from the rolling
# computation. 5 minutes balances "responsive enough to spot a stuck
# venue" against "smooth enough that a single 30 s blip doesn't dominate".
_WINDOW_S = 300.0
_MAX_SAMPLES_PER_EX = 600  # cap memory: 1 sample/s × 5 min × 2 safety margin

# {exchange: deque[(monotonic_ts, age_s)]}
_samples: dict[str, "deque[tuple[float, float]]"] = {}
_lock = threading.Lock()


def record(exchange: str, age_s: float | None) -> None:
    """Add one freshness sample. Called from the exchange-health endpoint
    every time it computes per-venue ages — no extra fetcher work."""
    if age_s is None or age_s < 0:
        return
    now = time.monotonic()
    with _lock:
        dq = _samples.get(exchange)
        if dq is None:
            dq = deque(maxlen=_MAX_SAMPLES_PER_EX)
            _samples[exchange] = dq
        dq.append((now, float(age_s)))
        # Evict samples that fell out of the window
        cutoff = now - _WINDOW_S
        while dq and dq[0][0] < cutoff:
            dq.popleft()


def stats(exchanges: Iterable[str] | None = None) -> dict:
    """Return per-exchange average + max + sample-count over the window,
    plus the cross-exchange overall average."""
    now = time.monotonic()
    cutoff = now - _WINDOW_S
    per: dict[str, dict] = {}
    all_ages: list[float] = []
    with _lock:
        keys = list(exchanges) if exchanges else list(_samples.keys())
        for ex in keys:
            dq = _samples.get(ex)
            if not dq:
                continue
            ages = [a for (t, a) in dq if t >= cutoff]
            if not ages:
                continue
            avg = sum(ages) / len(ages)
            mx = max(ages)
            mn = min(ages)
            per[ex] = {
                "avg_age_s": round(avg, 2),
                "max_age_s": round(mx, 2),
                "min_age_s": round(mn, 2),
                "samples": len(ages),
            }
            all_ages.extend(ages)
    overall_avg = round(sum(all_ages) / len(all_ages), 2) if all_ages else None
    overall_max = round(max(all_ages), 2) if all_ages else None
    return {
        "window_s": _WINDOW_S,
        "exchanges": per,
        "overall_avg_age_s": overall_avg,
        "overall_max_age_s": overall_max,
        "total_samples": len(all_ages),
    }
