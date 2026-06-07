"""Arb spread consumer + rollup + retention tests.

Stream consumer tests use a fake redis (no real Redis). Rollup logic
runs as pure SQL but is Postgres-only (window functions / GREATEST /
LEAST work different); we test parsing + batch shaping on SQLite, and
defer the rollup SQL itself to manual verification on Postgres.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-32-chars-long-aaaa")

import asyncio
import json
from unittest.mock import patch, AsyncMock


def _in_memory_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from backend.db.base import Base
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine), engine


def test_flag_off_consumer_is_noop():
    """When AVALANT_SPREAD_HISTORY=0 the consumer returns immediately
    without touching Redis. Critical so the flag actually disables the
    feature in production."""
    os.environ["AVALANT_SPREAD_HISTORY"] = "0"
    from backend.services import arb_spread_service
    assert arb_spread_service.is_enabled() is False
    # run_spread_consumer is a coroutine; running it should return None
    # without raising.
    asyncio.run(arb_spread_service.run_spread_consumer())


def test_flag_on_recognized():
    os.environ["AVALANT_SPREAD_HISTORY"] = "1"
    from backend.services import arb_spread_service
    assert arb_spread_service.is_enabled() is True


def test_process_batch_inserts_5s_rows():
    """Verify the batch INSERT actually lands rows in the 5s table on
    SQLite (uses INSERT OR REPLACE fallback). Postgres path uses
    ON CONFLICT DO UPDATE — covered by manual smoke on prod."""
    os.environ["AVALANT_SPREAD_HISTORY"] = "1"
    Session, _ = _in_memory_db()
    from backend.services import arb_spread_service
    with patch.object(arb_spread_service, "SessionLocal", Session):
        batch = [{
            "el": "binance", "es": "bybit", "sym": "BTC", "ts": 1700000000,
            "io": 0.05, "ih": 0.07, "il": 0.04, "ic": 0.06,
            "oo": -0.02, "oh": -0.01, "ol": -0.03, "oc": -0.02,
            "n": 12,
        }, {
            "el": "okx", "es": "gate", "sym": "ETH", "ts": 1700000000,
            "io": 0.10, "ih": 0.11, "il": 0.09, "ic": 0.10,
            "oo": -0.05, "oh": -0.04, "ol": -0.06, "oc": -0.05,
            "n": 8,
        }]
        n = asyncio.run(arb_spread_service._process_batch(batch))
        assert n == 2

        from sqlalchemy import text
        db = Session()
        try:
            rows = db.execute(text("SELECT exchange_long, symbol, samples "
                                    "FROM arb_spread_candles_5s "
                                    "ORDER BY symbol")).all()
            assert len(rows) == 2
            assert ("binance", "BTC", 12) in [(r[0], r[1], r[2]) for r in rows]
            assert ("okx", "ETH", 8) in [(r[0], r[1], r[2]) for r in rows]
        finally:
            db.close()


def test_process_batch_empty_no_op():
    os.environ["AVALANT_SPREAD_HISTORY"] = "1"
    Session, _ = _in_memory_db()
    from backend.services import arb_spread_service
    with patch.object(arb_spread_service, "SessionLocal", Session):
        n = asyncio.run(arb_spread_service._process_batch([]))
        assert n == 0


def test_redelivery_is_idempotent():
    """The PK is (exchange_long, exchange_short, symbol, bucket_ts). A
    redelivered stream entry over the same key must NOT raise — SQLite
    via INSERT OR REPLACE handles this; Postgres via ON CONFLICT DO
    UPDATE merges. Either way: second call returns same row count
    without error."""
    os.environ["AVALANT_SPREAD_HISTORY"] = "1"
    Session, _ = _in_memory_db()
    from backend.services import arb_spread_service
    with patch.object(arb_spread_service, "SessionLocal", Session):
        batch = [{
            "el": "binance", "es": "bybit", "sym": "BTC", "ts": 1700000000,
            "io": 0.05, "ih": 0.07, "il": 0.04, "ic": 0.06,
            "oo": -0.02, "oh": -0.01, "ol": -0.03, "oc": -0.02,
            "n": 12,
        }]
        asyncio.run(arb_spread_service._process_batch(batch))
        # Second insert — same key. Must not raise.
        asyncio.run(arb_spread_service._process_batch(batch))
        from sqlalchemy import text
        db = Session()
        try:
            n = db.execute(text("SELECT COUNT(*) FROM arb_spread_candles_5s")).scalar()
            assert n == 1   # single row, replaced
        finally:
            db.close()


def test_rollup_sqlite_noop():
    """Rollup uses Postgres-specific array_agg + GREATEST/LEAST. On
    SQLite the function early-returns 0 so test_setup doesn't crash."""
    os.environ["AVALANT_SPREAD_HISTORY"] = "1"
    Session, _ = _in_memory_db()
    from backend.services import arb_spread_service
    with patch.object(arb_spread_service, "SessionLocal", Session):
        n_1m = asyncio.run(arb_spread_service.rollup_5s_to_1m())
        n_1h = asyncio.run(arb_spread_service.rollup_1m_to_1h())
        assert n_1m == 0
        assert n_1h == 0


def test_prune_retention_sqlite_noop():
    os.environ["AVALANT_SPREAD_HISTORY"] = "1"
    Session, _ = _in_memory_db()
    from backend.services import arb_spread_service
    with patch.object(arb_spread_service, "SessionLocal", Session):
        out = asyncio.run(arb_spread_service.prune_retention())
        assert out == {"5s": 0, "1m": 0, "1h": 0}


def test_constants_match_go_fetcher():
    """STREAM_NAME and bucket sec must match go-fetcher's spread package
    (internal/spread/recorder.go). Drift would silently break the
    pipeline."""
    from backend.services import arb_spread_service
    assert arb_spread_service.STREAM_NAME == "arb:spread:bucket"
    assert arb_spread_service.TIER_5S_S == 5
    assert arb_spread_service.RETENTION_5S_S == 86400
    assert arb_spread_service.RETENTION_1M_S == 604800
    assert arb_spread_service.RETENTION_1H_S == 7776000
