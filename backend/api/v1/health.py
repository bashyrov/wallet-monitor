import os
import time

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/providers")
def provider_counts():
    """Public endpoint — returns how many providers of each type are supported."""
    from backend.api.v1.wallets import WALLET_OPTIONS
    opts = WALLET_OPTIONS
    return {
        "exchanges": len(opts["exchange_types"]),
        "chains": len(opts["chain_types"]),
        "perp_dexes": sum(1 for p in opts["perpdex_types"] if not p.get("soon")),
    }


@router.get("/health/feeds")
def health_feeds():
    """One-shot JSON describing the freshness of every market-data feed and
    cache file. Useful for ops / debugging — read once, see what's stale.

    Shape:
      {
        ts: <unix>,
        role: "web"|"fetcher",
        caches: { arbitrage.json: {exists, age_s}, spot_arbitrage.json: {...},
                  dex_arbitrage.json: {...}, funding.json: {...},
                  books.json: {...} },
        exchanges: { <ex>: {age_s, healthy, via} },   # per-venue funding
        orderbook: { <ex>: {age_s, healthy} },        # per-venue book freshness
        counts: { futures, spot_short, dex_short },
      }
    """
    now = time.time()
    out = {
        "ts": int(now),
        "role": os.environ.get("AVALANT_ROLE", "mono").lower(),
        "caches": {},
        "exchanges": {},
        "orderbook": {},
        "counts": {},
    }

    # File-cache freshness
    cache_dir = "/tmp/avalant_cache"
    for name in ("funding.json", "arbitrage.json",
                 "spot_arbitrage.json", "dex_arbitrage.json",
                 "books.json"):
        path = os.path.join(cache_dir, name)
        try:
            st = os.stat(path)
            out["caches"][name] = {
                "exists": True,
                "age_s": round(now - st.st_mtime, 2),
                "size_kb": round(st.st_size / 1024, 1),
            }
        except OSError:
            out["caches"][name] = {"exists": False}

    # Per-exchange funding feed freshness
    try:
        from backend.services.arbitrage_service import get_exchange_health
        out["exchanges"] = get_exchange_health()
    except Exception as e:
        out["exchanges_error"] = str(e)

    # Per-exchange orderbook freshness (if available)
    try:
        from backend.services.orderbook_cache import freshness_by_exchange
        out["orderbook"] = freshness_by_exchange() or {}
    except Exception as e:
        out["orderbook_error"] = str(e)

    # Circuit-breaker state (tripped venues + recent-failure counts)
    try:
        from backend.services._circuit import circuit
        cb = circuit.state()
        if cb:
            out["circuit"] = cb
    except Exception as e:
        out["circuit_error"] = str(e)

    # Opportunity counts from the latest cache snapshot
    try:
        from backend.services.arbitrage_service import _read_file_cache
        fut = _read_file_cache("arbitrage.json", max_age=120.0) or {}
        spot = _read_file_cache("spot_arbitrage.json", max_age=120.0) or {}
        dex = _read_file_cache("dex_arbitrage.json", max_age=120.0) or {}
        out["counts"] = {
            "futures": len(fut.get("opportunities") or []),
            "spot_short": len(spot.get("opportunities") or []),
            "dex_short": len(dex.get("opportunities") or []),
        }
    except Exception as e:
        out["counts_error"] = str(e)

    return out


@router.get("/health/fetcher")
def health_fetcher():
    """Per-worker status of the multiprocess fetcher (M5 observability).

    Reads /tmp/avalant_cache/fetcher_workers.json written by the orchestrator
    every 5s. Falls back to `{mode: "single"}` when the fetcher runs in the
    legacy single-process mode (no workers, nothing to observe here — use
    /health/feeds instead).
    """
    from backend.services.orderbook_ws_master import read_workers_health
    data = read_workers_health()
    if not data:
        return {"mode": "single", "note": "set AVALANT_FETCHER_MODE=multiprocess to enable"}
    workers = data.get("workers") or []
    now = time.time()
    # Pair each worker with its books.<ex>.json freshness so ops can spot
    # "process alive but WS stream dead" cases.
    for w in workers:
        ex = w.get("exchange") or ""
        path = os.path.join("/tmp/avalant_cache", f"books.{ex}.json")
        try:
            st = os.stat(path)
            w["books_file_age_s"] = round(now - st.st_mtime, 2)
            w["books_file_size"] = st.st_size
        except OSError:
            w["books_file_age_s"] = None
            w["books_file_size"] = 0
    return {
        "mode": "multiprocess",
        "snapshot_age_s": round(now - (data.get("ts") or 0), 1) if data.get("ts") else None,
        "workers": workers,
    }
