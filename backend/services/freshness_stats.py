"""Rolling per-exchange freshness statistics for the admin dashboard.

`get_exchange_health` already reports the *current* age_s per venue;
this module remembers a rolling 5-minute window of those samples so the
admin page can show "average freshness" — the metric users actually
care about (a venue can spike to 30s once and recover; an average tells
us whether it's chronically slow).

A background sampler thread on the fetcher polls get_exchange_health
every 3s and writes stats() to /tmp/avalant_cache/freshness_stats.json.
The admin endpoint on web replicas reads the file (cross-process), so
the dashboard reflects what the sampler thread has recorded regardless
of which web replica handles the GET.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Iterable

logger = logging.getLogger("avalant.freshness_stats")
_CACHE_DIR = os.environ.get("AVALANT_CACHE_DIR", "/tmp/avalant_cache")
_STATS_FILE = f"{_CACHE_DIR}/freshness_stats.json"

# Window length — ages older than this are dropped from the rolling
# computation. 60 s is responsive: a 30s spike still drags the avg up
# noticeably but doesn't swamp the dashboard for the next 5 min.
_WINDOW_S = 60.0
_MAX_SAMPLES_PER_EX = 120  # cap memory: 1 sample/s × 60 s × 2 safety margin

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


_sampler_thread: threading.Thread | None = None
_sampler_stop = threading.Event()


def _sampler_loop() -> None:
    """Sample get_exchange_health() every 3s, persist via file cache so the
    admin endpoint (potentially on a different replica) can read it."""
    from backend.services.arbitrage_service import get_exchange_health
    logger.info("freshness sampler thread started (interval=3s)")
    last_write = 0.0
    tick = 0
    while not _sampler_stop.is_set():
        tick += 1
        try:
            health = get_exchange_health() or {}
            valid_ages = sum(1 for v in health.values() if v.get("age_s") is not None)
            if tick % 10 == 1:
                logger.info("freshness sampler tick=%d venues_with_age=%d/%d",
                            tick, valid_ages, len(health))
            # `record` is already called inside get_exchange_health, so we
            # just need to flush stats() to disk once per cycle.
            now = time.time()
            if now - last_write >= 2.0:
                last_write = now
                snapshot = stats()
                snapshot["written_at"] = now
                try:
                    os.makedirs(_CACHE_DIR, exist_ok=True)
                    tmp = _STATS_FILE + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump(snapshot, f)
                    os.replace(tmp, _STATS_FILE)
                except Exception as exc:
                    logger.warning("freshness stats write failed: %s", exc)
        except Exception as exc:
            logger.warning("freshness sampler tick failed: %s", exc)
        _sampler_stop.wait(3.0)


def start_sampler() -> None:
    """Idempotent — starts the background sampler if not already running.
    Called once from the fetcher entrypoint."""
    global _sampler_thread
    if _sampler_thread is not None and _sampler_thread.is_alive():
        return
    _sampler_thread = threading.Thread(target=_sampler_loop, name="freshness-sampler", daemon=True)
    _sampler_thread.start()


def read_persisted_stats() -> dict:
    """Read the snapshot the fetcher's sampler thread last wrote. Used by
    the admin endpoint on web replicas (which have their own empty
    in-memory _samples dict)."""
    try:
        with open(_STATS_FILE, "rb") as f:
            return json.loads(f.read())
    except FileNotFoundError:
        return {"window_s": _WINDOW_S, "exchanges": {}, "overall_avg_age_s": None,
                "overall_max_age_s": None, "total_samples": 0, "written_at": None}
    except Exception as exc:
        logger.warning("freshness stats read failed: %s", exc)
        return {"window_s": _WINDOW_S, "exchanges": {}, "overall_avg_age_s": None,
                "overall_max_age_s": None, "total_samples": 0, "written_at": None}


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
