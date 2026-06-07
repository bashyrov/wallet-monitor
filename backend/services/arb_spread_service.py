"""Arb spread time-series consumer + rollup + retention.

Pipeline:
  go-fetcher arb compute (5 Hz) → 5s in-memory OHLC bucket
  → flush every 5s to Redis stream arb:spread:bucket (XADD)
  → THIS consumer batch-INSERTs into arb_spread_candles_5s
  → rollup task aggregates 5s→1m (hourly) and 1m→1h (daily)
  → retention pruner deletes 5s>24h, 1m>7d, 1h>90d (nightly 03:00 UTC)

Entry: `await run_spread_consumer()` — spawned once per web replica.
Both replicas can run; XREADGROUP+ACK ensures each stream entry is
processed exactly once. Without group support we'd double-write
(idempotent thanks to ON CONFLICT, but wasteful).

Behind AVALANT_SPREAD_HISTORY=1 — same flag as go-fetcher write side.
Off = consumer never starts; rollups + retention are no-ops.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from backend.db.base import SessionLocal

logger = logging.getLogger("avalant.arb_spread")

# Same constants as go-fetcher's internal/spread package — keep in sync.
STREAM_NAME = "arb:spread:bucket"
CONSUMER_GROUP = "spread-consumer"

# Tiers + retention. Read by both rollup and pruner.
TIER_5S_S = 5
TIER_1M_S = 60
TIER_1H_S = 3600
RETENTION_5S_S = 24 * 3600          # 24h
RETENTION_1M_S = 7 * 24 * 3600       # 7d
RETENTION_1H_S = 90 * 24 * 3600      # 90d

# Batch insert size — 500 buckets/INSERT keeps statement size small
# enough for PgBouncer (default max 16k bytes per packet) and the
# round-trip cheap.
BATCH_SIZE = 500
# Max time we'll buffer before flushing even if BATCH_SIZE not reached.
# 1s = same TICK that go-fetcher writes at, so consumer + producer rate-match.
BATCH_FLUSH_MS = 1000


def is_enabled() -> bool:
    return (os.getenv("AVALANT_SPREAD_HISTORY") or "0").strip() == "1"


def _redis():
    """Lazy-init Redis client. Reads REDIS_URL; raises if unset."""
    url = os.environ.get("REDIS_URL")
    if not url:
        raise RuntimeError("REDIS_URL required for spread consumer")
    import redis.asyncio as aioredis
    return aioredis.from_url(url, decode_responses=True,
                             socket_connect_timeout=2.0,
                             socket_keepalive=True)


async def _ensure_group(client) -> None:
    """Create the consumer group if it doesn't exist. Idempotent — replicas
    starting in parallel both call this; one wins, the other gets BUSYGROUP
    which we swallow."""
    try:
        await client.xgroup_create(STREAM_NAME, CONSUMER_GROUP,
                                   id="0", mkstream=True)
        logger.info("created consumer group %s on %s",
                    CONSUMER_GROUP, STREAM_NAME)
    except Exception as exc:
        if "BUSYGROUP" in str(exc):
            return
        raise


async def _process_batch(batch: list[dict]) -> int:
    """ON CONFLICT upsert: a re-delivered stream entry (after consumer
    crash without XACK) updates the existing row instead of erroring.
    The bucket_ts is the dedup key + the new values reflect the latest
    flush of that window."""
    if not batch:
        return 0
    db = SessionLocal()
    try:
        # Multi-row INSERT ... ON CONFLICT (PK) DO UPDATE — single
        # round-trip per batch. Postgres specific; SQLite tests use
        # INSERT OR REPLACE via dialect dispatch (see test fixture).
        dialect = db.bind.dialect.name
        if dialect == "postgresql":
            stmt = text("""
                INSERT INTO arb_spread_candles_5s
                  (exchange_long, exchange_short, symbol, bucket_ts,
                   in_open, in_high, in_low, in_close,
                   out_open, out_high, out_low, out_close, samples)
                VALUES
                  (:el, :es, :sym, :ts,
                   :io, :ih, :il, :ic,
                   :oo, :oh, :ol, :oc, :n)
                ON CONFLICT (exchange_long, exchange_short, symbol, bucket_ts)
                DO UPDATE SET
                  in_high  = GREATEST(EXCLUDED.in_high,  arb_spread_candles_5s.in_high),
                  in_low   = LEAST   (EXCLUDED.in_low,   arb_spread_candles_5s.in_low),
                  in_close = EXCLUDED.in_close,
                  out_high = GREATEST(EXCLUDED.out_high, arb_spread_candles_5s.out_high),
                  out_low  = LEAST   (EXCLUDED.out_low,  arb_spread_candles_5s.out_low),
                  out_close = EXCLUDED.out_close,
                  samples  = arb_spread_candles_5s.samples + EXCLUDED.samples
            """)
        else:
            # SQLite fallback for tests. INSERT OR REPLACE drops the
            # row's hi/lo accumulation — acceptable for tests, never
            # runs in prod.
            stmt = text("""
                INSERT OR REPLACE INTO arb_spread_candles_5s
                  (exchange_long, exchange_short, symbol, bucket_ts,
                   in_open, in_high, in_low, in_close,
                   out_open, out_high, out_low, out_close, samples)
                VALUES
                  (:el, :es, :sym, :ts,
                   :io, :ih, :il, :ic,
                   :oo, :oh, :ol, :oc, :n)
            """)
        db.execute(stmt, batch)
        db.commit()
        return len(batch)
    finally:
        db.close()


async def run_spread_consumer() -> None:
    """Main consumer loop. Reads stream → buffers → batch INSERT. One
    instance per replica; consumer group ensures each entry handled
    once across replicas."""
    if not is_enabled():
        logger.info("spread consumer disabled (AVALANT_SPREAD_HISTORY!=1)")
        return
    client = _redis()
    await _ensure_group(client)
    consumer_name = f"web-{socket.gethostname()}-{os.getpid()}"
    logger.info("spread consumer starting: name=%s", consumer_name)

    buffer: list[dict] = []
    pending_ack: list[str] = []
    last_flush = asyncio.get_event_loop().time()

    async def _flush() -> None:
        nonlocal buffer, pending_ack, last_flush
        if not buffer:
            return
        try:
            n = await _process_batch(buffer)
            await client.xack(STREAM_NAME, CONSUMER_GROUP, *pending_ack)
            logger.debug("flushed %d buckets (acked %d)", n, len(pending_ack))
        except Exception as exc:
            logger.warning("spread batch flush failed: %s (will retry)", exc)
            # Don't ACK on failure — entries stay claimed. Next XREADGROUP
            # call will re-deliver them (after PEL claim or BLOCK=0 wakeup).
            return
        buffer = []
        pending_ack = []
        last_flush = asyncio.get_event_loop().time()

    while True:
        try:
            # BLOCK with short timeout so we still flush on time even when
            # the stream is quiet.
            res = await client.xreadgroup(
                CONSUMER_GROUP, consumer_name,
                streams={STREAM_NAME: ">"},
                count=BATCH_SIZE, block=BATCH_FLUSH_MS,
            )
            if res:
                for _stream, entries in res:
                    for entry_id, data in entries:
                        raw = data.get("d")
                        if not raw:
                            pending_ack.append(entry_id)
                            continue
                        try:
                            obj = json.loads(raw)
                        except Exception:
                            pending_ack.append(entry_id)
                            continue
                        buffer.append({
                            "el": str(obj.get("el") or "")[:16],
                            "es": str(obj.get("es") or "")[:16],
                            "sym": str(obj.get("sym") or "")[:32],
                            "ts": int(obj.get("ts") or 0),
                            "io": float(obj.get("io") or 0),
                            "ih": float(obj.get("ih") or 0),
                            "il": float(obj.get("il") or 0),
                            "ic": float(obj.get("ic") or 0),
                            "oo": float(obj.get("oo") or 0),
                            "oh": float(obj.get("oh") or 0),
                            "ol": float(obj.get("ol") or 0),
                            "oc": float(obj.get("oc") or 0),
                            "n": int(obj.get("n") or 1),
                        })
                        pending_ack.append(entry_id)
            # Time- or size-triggered flush.
            now = asyncio.get_event_loop().time()
            if buffer and (
                len(buffer) >= BATCH_SIZE
                or (now - last_flush) * 1000 >= BATCH_FLUSH_MS
            ):
                await _flush()
        except asyncio.CancelledError:
            await _flush()
            raise
        except Exception as exc:
            logger.warning("spread consumer loop error: %s — retrying in 2s", exc)
            await asyncio.sleep(2.0)


# ── Rollup helpers ──────────────────────────────────────────────────────

async def rollup_5s_to_1m(*, until_ts: int | None = None) -> int:
    """Aggregate completed 5s candles into 1m candles.

    `until_ts` = inclusive upper bound (default now − 60s = previous
    completed minute). Idempotent via ON CONFLICT DO NOTHING — a re-run
    over the same window is a no-op. 1m bucket = floor(t/60)*60.
    """
    if not is_enabled():
        return 0
    if until_ts is None:
        until_ts = int(_now_ts() / 60) * 60 - 60   # previous completed minute
    db = SessionLocal()
    try:
        if db.bind.dialect.name != "postgresql":
            return 0   # SQLite test path skips rollup (covered separately)
        # Aggregate 12 5s candles → 1 1m candle. Open = first by ts,
        # close = last by ts, high/low = max/min of highs/lows. Window
        # functions keep this single-pass.
        stmt = text("""
            INSERT INTO arb_spread_candles_1m
              (exchange_long, exchange_short, symbol, bucket_ts,
               in_open, in_high, in_low, in_close,
               out_open, out_high, out_low, out_close, samples)
            SELECT
              exchange_long, exchange_short, symbol,
              (bucket_ts / 60) * 60 AS bucket_ts,
              (array_agg(in_open  ORDER BY bucket_ts ASC ))[1] AS in_open,
              MAX(in_high) AS in_high,
              MIN(in_low)  AS in_low,
              (array_agg(in_close ORDER BY bucket_ts DESC))[1] AS in_close,
              (array_agg(out_open  ORDER BY bucket_ts ASC ))[1] AS out_open,
              MAX(out_high) AS out_high,
              MIN(out_low)  AS out_low,
              (array_agg(out_close ORDER BY bucket_ts DESC))[1] AS out_close,
              SUM(samples)::smallint AS samples
            FROM arb_spread_candles_5s
            WHERE bucket_ts <= :until_ts
              AND bucket_ts >= :since_ts
            GROUP BY exchange_long, exchange_short, symbol, (bucket_ts / 60) * 60
            ON CONFLICT (exchange_long, exchange_short, symbol, bucket_ts) DO NOTHING
        """)
        # Only process recent minutes — older 5s already rolled, no need to
        # re-scan. 2h window covers any cron-skew + manual replays.
        result = db.execute(stmt, {
            "until_ts": until_ts,
            "since_ts": until_ts - 2 * 3600,
        })
        db.commit()
        return result.rowcount or 0
    finally:
        db.close()


async def rollup_1m_to_1h(*, until_ts: int | None = None) -> int:
    """Aggregate 60 completed 1m candles into 1 1h candle. Idempotent."""
    if not is_enabled():
        return 0
    if until_ts is None:
        until_ts = int(_now_ts() / 3600) * 3600 - 3600
    db = SessionLocal()
    try:
        if db.bind.dialect.name != "postgresql":
            return 0
        stmt = text("""
            INSERT INTO arb_spread_candles_1h
              (exchange_long, exchange_short, symbol, bucket_ts,
               in_open, in_high, in_low, in_close,
               out_open, out_high, out_low, out_close, samples)
            SELECT
              exchange_long, exchange_short, symbol,
              (bucket_ts / 3600) * 3600 AS bucket_ts,
              (array_agg(in_open  ORDER BY bucket_ts ASC ))[1] AS in_open,
              MAX(in_high) AS in_high,
              MIN(in_low)  AS in_low,
              (array_agg(in_close ORDER BY bucket_ts DESC))[1] AS in_close,
              (array_agg(out_open  ORDER BY bucket_ts ASC ))[1] AS out_open,
              MAX(out_high) AS out_high,
              MIN(out_low)  AS out_low,
              (array_agg(out_close ORDER BY bucket_ts DESC))[1] AS out_close,
              LEAST(SUM(samples), 32767)::smallint AS samples
            FROM arb_spread_candles_1m
            WHERE bucket_ts <= :until_ts
              AND bucket_ts >= :since_ts
            GROUP BY exchange_long, exchange_short, symbol, (bucket_ts / 3600) * 3600
            ON CONFLICT (exchange_long, exchange_short, symbol, bucket_ts) DO NOTHING
        """)
        result = db.execute(stmt, {
            "until_ts": until_ts,
            "since_ts": until_ts - 7 * 24 * 3600,
        })
        db.commit()
        return result.rowcount or 0
    finally:
        db.close()


async def prune_retention() -> dict[str, int]:
    """Delete rows beyond per-tier retention. Run nightly."""
    if not is_enabled():
        return {"5s": 0, "1m": 0, "1h": 0}
    db = SessionLocal()
    try:
        if db.bind.dialect.name != "postgresql":
            return {"5s": 0, "1m": 0, "1h": 0}
        now = _now_ts()
        out = {}
        for table, retention_s in (
            ("arb_spread_candles_5s", RETENTION_5S_S),
            ("arb_spread_candles_1m", RETENTION_1M_S),
            ("arb_spread_candles_1h", RETENTION_1H_S),
        ):
            cutoff = now - retention_s
            result = db.execute(
                text(f"DELETE FROM {table} WHERE bucket_ts < :cutoff"),
                {"cutoff": cutoff},
            )
            out[table.split("_")[-1]] = result.rowcount or 0
        db.commit()
        return out
    finally:
        db.close()


# ── Daemon scheduler ────────────────────────────────────────────────────

async def run_rollup_daemon() -> None:
    """Periodic rollup + retention. Both replicas run this; ON CONFLICT
    DO NOTHING ensures the second one is a no-op (correct + cheap)."""
    if not is_enabled():
        return
    while True:
        try:
            # 5s→1m every minute (lagging by 60s so the minute is complete).
            n_1m = await rollup_5s_to_1m()
            if n_1m:
                logger.info("rollup 5s→1m: %d rows", n_1m)
            # 1m→1h every minute too — cheap when nothing's new (ON CONFLICT).
            n_1h = await rollup_1m_to_1h()
            if n_1h:
                logger.info("rollup 1m→1h: %d rows", n_1h)
            # Retention pruning once per day. Trigger on minute-of-day to
            # avoid both replicas pruning simultaneously (one wins via DB
            # row lock, the other no-ops).
            if datetime.utcnow().hour == 3 and datetime.utcnow().minute == 0:
                pruned = await prune_retention()
                logger.info("retention prune: %s", pruned)
        except Exception as exc:
            logger.warning("rollup daemon iteration failed: %s", exc)
        await asyncio.sleep(60)


def _now_ts() -> int:
    return int(datetime.utcnow().timestamp())
