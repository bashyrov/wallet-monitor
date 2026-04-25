import os
import time

from fastapi import APIRouter, Response

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/maintenance/status")
def maintenance_status():
    """Public — single source of truth the maintenance/section-blocked HTML
    pages poll every 15 s to auto-reload when ops flips a flag back off
    (or when the ETA passes). Always returns 200 even during full-site
    maintenance so the page can recover without a manual refresh."""
    from backend.services import admin_settings as _s
    return {
        "maintenance":         _s.is_maintenance(),
        "screener_disabled":   _s.is_screener_disabled(),
        "portfolio_disabled":  _s.is_portfolio_disabled(),
        "ends_at":             _s.get_maintenance_ends_at(),
        "screener_ends_at":    _s.get_screener_disabled_ends_at(),
        "portfolio_ends_at":   _s.get_portfolio_disabled_ends_at(),
        "tz":                  _s.get_maintenance_tz(),
        "ts":                  int(time.time()),
    }


@router.get("/metrics")
def metrics():
    """Prometheus-format metrics endpoint — plaintext, no deps.

    Exposed:
      · avalant_fetcher_mode            {mode}           info
      · avalant_orderbook_fresh_count   {exchange}       gauge, # of fresh pairs
      · avalant_orderbook_min_age_s     {exchange}       gauge, freshest book age
      · avalant_funding_age_s           {exchange}       gauge, per-venue funding staleness
      · avalant_opp_count               {type}           gauge, arb opps per feed
      · avalant_fetcher_worker_alive    {kind,exchange}  gauge, 1 or 0
      · avalant_fetcher_worker_uptime_s {kind,exchange}  gauge

    Scrape with:
      scrape_config:
        - job_name: avalant
          static_configs: [{ targets: ['avalant.xyz'] }]
          metrics_path: /api/metrics
          scheme: https
    """
    lines: list[str] = []

    def _gauge(name: str, help_: str) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} gauge")

    # Fetcher mode
    from backend.services.orderbook_ws_master import is_multiprocess_mode
    _gauge("avalant_fetcher_mode", "1 when multiprocess fetcher is active, 0 otherwise")
    lines.append(f"avalant_fetcher_mode {1 if is_multiprocess_mode() else 0}")

    # Orderbook freshness per exchange
    try:
        from backend.services.orderbook_cache import freshness_by_exchange
        fr = freshness_by_exchange() or {}
        _gauge("avalant_orderbook_fresh_count", "Number of pairs with orderbook age ≤ 5s")
        for ex, v in sorted(fr.items()):
            lines.append(f'avalant_orderbook_fresh_count{{exchange="{ex}"}} {int(v.get("fresh") or 0)}')
        _gauge("avalant_orderbook_min_age_s", "Age of the freshest book on this exchange (seconds)")
        for ex, v in sorted(fr.items()):
            age = v.get("min_age_s")
            if isinstance(age, (int, float)) and age < 1e9:
                lines.append(f'avalant_orderbook_min_age_s{{exchange="{ex}"}} {age:.3f}')
    except Exception:
        pass

    # Funding freshness
    try:
        from backend.services.arbitrage_service import get_exchange_health
        eh = get_exchange_health() or {}
        _gauge("avalant_funding_age_s", "Per-venue funding feed staleness (seconds)")
        for ex, v in sorted(eh.items()):
            age = v.get("age_s")
            if isinstance(age, (int, float)):
                lines.append(f'avalant_funding_age_s{{exchange="{ex}"}} {age:.2f}')
    except Exception:
        pass

    # Opp counts
    try:
        from backend.services.arbitrage_service import _read_file_cache
        _gauge("avalant_opp_count", "Opportunity count per arbitrage feed")
        for label, fname in (
            ("futures", "arbitrage.json"),
            ("spot_short", "spot_arbitrage.json"),
            ("dex_short", "dex_arbitrage.json"),
        ):
            d = _read_file_cache(fname, max_age=120.0) or {}
            lines.append(f'avalant_opp_count{{type="{label}"}} {len(d.get("opportunities") or [])}')
    except Exception:
        pass

    # Fetcher workers
    try:
        from backend.services.orderbook_ws_master import read_workers_health
        wh = read_workers_health()
        _gauge("avalant_fetcher_worker_alive", "1 if the worker subprocess is alive")
        _gauge("avalant_fetcher_worker_uptime_s", "Worker uptime since last respawn")
        _gauge("avalant_fetcher_worker_restarts_1m", "Worker restarts in the last 60s")
        for w in wh.get("workers") or []:
            kind = w.get("kind") or "?"
            ex = w.get("exchange") or "?"
            labels = f'{{kind="{kind}",exchange="{ex}"}}'
            lines.append(f'avalant_fetcher_worker_alive{labels} {1 if w.get("alive") else 0}')
            lines.append(f'avalant_fetcher_worker_uptime_s{labels} {w.get("uptime_s") or 0}')
            lines.append(f'avalant_fetcher_worker_restarts_1m{labels} {w.get("restarts_1m") or 0}')
    except Exception:
        pass

    # Live-mode observability: screener WS client counts + token registry size.
    # Used to confirm the post-upgrade broadcast cadence (0.3 s) is being
    # serviced to every connected client without backpressure.
    try:
        from backend.api.v1.screener import _funding_clients, _arb_clients, _book_ws_subs
        _gauge("avalant_ws_clients", "Currently-connected WS clients per channel")
        lines.append(f'avalant_ws_clients{{channel="funding"}} {len(_funding_clients)}')
        lines.append(f'avalant_ws_clients{{channel="arb"}} {len(_arb_clients)}')
        lines.append(f'avalant_ws_clients{{channel="book"}} {len(_book_ws_subs)}')
    except Exception:
        pass

    # Token registry size (PR #102 ticker-collision guard) — number of
    # symbols with on-chain contract info per exchange.
    try:
        from backend.services.token_registry import registry_snapshot
        snap = registry_snapshot() or {}
        ex_map = snap.get("exchanges") or {}
        _gauge("avalant_token_registry_symbols", "Symbols with on-chain contract info per exchange")
        for ex, count in sorted(ex_map.items()):
            lines.append(f'avalant_token_registry_symbols{{exchange="{ex}"}} {count}')
        age_s = snap.get("age_s")
        if isinstance(age_s, (int, float)):
            _gauge("avalant_token_registry_age_s", "Seconds since last token registry refresh")
            lines.append(f'avalant_token_registry_age_s {age_s:.1f}')
    except Exception:
        pass

    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4")


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
    # Pair each worker with its output file's freshness so ops can spot
    # "process alive but WS stream dead" cases. Orderbook workers dump
    # books.<ex>.json; funding workers dump funding_ws.<ex>.json.
    for w in workers:
        ex = w.get("exchange") or ""
        kind = w.get("kind") or "orderbook"
        fname = f"funding_ws.{ex}.json" if kind == "funding" else f"books.{ex}.json"
        path = os.path.join("/tmp/avalant_cache", fname)
        try:
            st = os.stat(path)
            w["output_file"] = fname
            w["output_age_s"] = round(now - st.st_mtime, 2)
            w["output_size"] = st.st_size
        except OSError:
            w["output_file"] = fname
            w["output_age_s"] = None
            w["output_size"] = 0
    return {
        "mode": "multiprocess",
        "snapshot_age_s": round(now - (data.get("ts") or 0), 1) if data.get("ts") else None,
        "workers": workers,
    }
