"""Exchange Health Monitor — background task measures latency + success rate per exchange."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.db.base import SessionLocal
from backend.db.models import ExchangeHealth
from backend.services.arbitrage_service import _http

logger = logging.getLogger("avalant.health")

PROBES = {
    "binance":     "https://fapi.binance.com/fapi/v1/ping",
    "bybit":       "https://api.bybit.com/v5/market/time",
    "okx":         "https://www.okx.com/api/v5/public/time",
    "gate":        "https://api.gateio.ws/api/v4/spot/time",
    "mexc":        "https://contract.mexc.com/api/v1/contract/ping",
    "kucoin":      "https://api-futures.kucoin.com/api/v1/timestamp",
    "bitget":      "https://api.bitget.com/api/v2/public/time",
    "hyperliquid": "https://api.hyperliquid.xyz/info",
    "aster":       "https://fapi.asterdex.com/fapi/v1/ping",
    "bingx":       "https://open-api.bingx.com/openApi/swap/v2/server/time",
    "lighter":     "https://mainnet.zklighter.elliot.ai/api/v1/status",
    "ethereal":    "https://api.etherealtest.net/v1/exchange-config",
    "whitebit":    "https://whitebit.com/api/v4/public/time",
}


async def _probe(exchange: str, url: str) -> tuple[int, bool, str | None]:
    start = time.perf_counter()
    try:
        if exchange == "hyperliquid":
            r = await _http.post(url, json={"type": "meta"}, headers={"Content-Type": "application/json"}, timeout=5.0)
        else:
            r = await _http.get(url, timeout=5.0)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        ok = 200 <= r.status_code < 300
        return elapsed_ms, ok, None if ok else f"HTTP {r.status_code}"
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return elapsed_ms, False, str(e)[:200]


async def probe_all() -> list[dict]:
    tasks = [_probe(ex, url) for ex, url in PROBES.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    rows: list[dict] = []
    db: Session = SessionLocal()
    try:
        for (ex, _), res in zip(PROBES.items(), results):
            if isinstance(res, Exception):
                latency, ok, err = 0, False, str(res)[:200]
            else:
                latency, ok, err = res
            db.add(ExchangeHealth(exchange=ex, latency_ms=latency, ok=ok, error=err))
            rows.append({"exchange": ex, "latency_ms": latency, "ok": ok, "error": err})
        # Prune: keep only last 7 days
        cutoff = datetime.utcnow() - timedelta(days=7)
        db.query(ExchangeHealth).filter(ExchangeHealth.ts < cutoff).delete()
        db.commit()
    finally:
        db.close()
    return rows


def summary(db: Session, window_min: int = 60) -> list[dict]:
    cutoff = datetime.utcnow() - timedelta(minutes=window_min)
    rows = db.query(ExchangeHealth).filter(ExchangeHealth.ts >= cutoff).all()
    by_ex: dict[str, list[ExchangeHealth]] = {}
    for r in rows:
        by_ex.setdefault(r.exchange, []).append(r)

    out: list[dict] = []
    for ex, items in by_ex.items():
        if not items:
            continue
        ok_count = sum(1 for x in items if x.ok)
        success_rate = ok_count / len(items) * 100
        latencies = sorted(x.latency_ms for x in items if x.ok)
        p50 = latencies[len(latencies) // 2] if latencies else 0
        p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0
        last = max(items, key=lambda x: x.ts)
        status = "healthy" if success_rate >= 95 else ("degraded" if success_rate >= 70 else "down")
        out.append({
            "exchange": ex,
            "success_rate_pct": round(success_rate, 1),
            "latency_p50_ms": p50,
            "latency_p95_ms": p95,
            "last_ok": last.ok,
            "last_latency_ms": last.latency_ms,
            "last_error": last.error,
            "last_ts": last.ts.isoformat(),
            "sample_count": len(items),
            "status": status,
        })
    out.sort(key=lambda x: x["exchange"])
    return out


async def health_loop(interval_s: int = 60):
    logger.info("Exchange health monitor started (interval=%ss)", interval_s)
    while True:
        try:
            await probe_all()
        except Exception as e:
            logger.error("Health probe failed: %s", e)
        await asyncio.sleep(interval_s)
