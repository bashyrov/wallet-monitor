"""GET /api/screener/arb-spread-history — TF selection + retention bump logic.

Endpoint pulls candles from 3 tier tables (5s/1m/1h). With tf=auto the
server picks the tier by span. With explicit tf= the server honours it
unless the span exceeds that tier's retention, in which case it bumps
up to a coarser tier rather than truncating.

We test the TF selection logic at the unit level (no Redis, no
Postgres) via direct SessionLocal injection. Postgres-only rollup logic
covered by manual smoke on prod.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-32-chars-long-aaaa")

import asyncio
import time
from unittest.mock import patch


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
    return sessionmaker(bind=engine)


def _seed(Session, table: str, rows: list[dict]):
    from sqlalchemy import text
    db = Session()
    try:
        for r in rows:
            db.execute(text(f"""
                INSERT INTO {table}
                  (exchange_long, exchange_short, symbol, bucket_ts,
                   in_open, in_high, in_low, in_close,
                   out_open, out_high, out_low, out_close, samples)
                VALUES (:el, :es, :sym, :ts, :io, :ih, :il, :ic,
                        :oo, :oh, :ol, :oc, :n)
            """), r)
        db.commit()
    finally:
        db.close()


def _call(**kwargs):
    """Invoke the endpoint coroutine directly with patched SessionLocal."""
    from backend.api.v1 import screener
    Session = _in_memory_db()
    with patch("backend.db.base.SessionLocal", Session):
        # The endpoint imports SessionLocal lazily inside the function.
        return asyncio.run(screener.arb_spread_history(**kwargs))


def _row(ts=1700000000):
    return {
        "el": "binance", "es": "bybit", "sym": "BTC", "ts": ts,
        "io": 0.05, "ih": 0.07, "il": 0.04, "ic": 0.06,
        "oo": -0.02, "oh": -0.01, "ol": -0.03, "oc": -0.02,
        "n": 12,
    }


# ── tf=auto: short span → 5s ───────────────────────────────────────────
def test_auto_picks_5s_for_short_span():
    Session = _in_memory_db()
    now = int(time.time())
    _seed(Session, "arb_spread_candles_5s",
          [_row(ts=now - i*5) for i in range(10)])
    with patch("backend.db.base.SessionLocal", Session):
        from backend.api.v1 import screener
        res = asyncio.run(screener.arb_spread_history(
            symbol="BTC", long="binance", short="bybit",
            tf="auto", from_ts=now - 60, to_ts=now,
        ))
    assert res["tf"] == "5s"


# ── tf=auto: medium span → 1m ──────────────────────────────────────────
def test_auto_picks_1m_for_medium_span():
    Session = _in_memory_db()
    now = int(time.time())
    _seed(Session, "arb_spread_candles_1m",
          [_row(ts=(now // 60) * 60 - i*60) for i in range(10)])
    with patch("backend.db.base.SessionLocal", Session):
        from backend.api.v1 import screener
        res = asyncio.run(screener.arb_spread_history(
            symbol="BTC", long="binance", short="bybit",
            tf="auto", from_ts=now - 30 * 60, to_ts=now,
        ))
    assert res["tf"] == "1m"


# ── tf=auto: huge span → 1h ────────────────────────────────────────────
def test_auto_picks_1h_for_huge_span():
    Session = _in_memory_db()
    now = int(time.time())
    _seed(Session, "arb_spread_candles_1h",
          [_row(ts=(now // 3600) * 3600 - i*3600) for i in range(10)])
    with patch("backend.db.base.SessionLocal", Session):
        from backend.api.v1 import screener
        res = asyncio.run(screener.arb_spread_history(
            symbol="BTC", long="binance", short="bybit",
            tf="auto", from_ts=now - 7 * 86400, to_ts=now,
        ))
    assert res["tf"] == "1h"


# ── Explicit tf=5s but span exceeds 24h → bump to 1m ───────────────────
def test_retention_bump_5s_to_1m_when_span_too_long():
    Session = _in_memory_db()
    now = int(time.time())
    _seed(Session, "arb_spread_candles_1m",
          [_row(ts=(now // 60) * 60 - i*60) for i in range(3)])
    with patch("backend.db.base.SessionLocal", Session):
        from backend.api.v1 import screener
        res = asyncio.run(screener.arb_spread_history(
            symbol="BTC", long="binance", short="bybit",
            tf="5s", from_ts=now - 5 * 86400, to_ts=now,
        ))
    # 5s retention is 24h; 5-day span forces bump → 1m or 1h.
    assert res["tf"] in ("1m", "1h")


# ── Span >1500 candles at chosen tier forces bump up ───────────────────
def test_span_over_1500_candles_bumps_tier():
    Session = _in_memory_db()
    now = int(time.time())
    _seed(Session, "arb_spread_candles_1m",
          [_row(ts=(now // 60) * 60 - i*60) for i in range(3)])
    with patch("backend.db.base.SessionLocal", Session):
        from backend.api.v1 import screener
        res = asyncio.run(screener.arb_spread_history(
            symbol="BTC", long="binance", short="bybit",
            tf="5s",  # would yield ~3600 candles for 5h
            from_ts=now - 5 * 3600, to_ts=now,
        ))
    # 5s estimate = 5h / 5s = 3600 candles → bump to 1m.
    assert res["tf"] == "1m"


# ── from >= to → empty candles, no SQL run ─────────────────────────────
def test_inverted_range_returns_empty():
    with patch("backend.db.base.SessionLocal", _in_memory_db()):
        from backend.api.v1 import screener
        res = asyncio.run(screener.arb_spread_history(
            symbol="BTC", long="binance", short="bybit",
            tf="auto", from_ts=1700000100, to_ts=1700000000,
        ))
    assert res["candles"] == []


# ── Candle shape: each row has all 9 OHLC fields + samples ─────────────
def test_candle_response_shape():
    Session = _in_memory_db()
    now = int(time.time())
    _seed(Session, "arb_spread_candles_5s", [_row(ts=now - 5)])
    with patch("backend.db.base.SessionLocal", Session):
        from backend.api.v1 import screener
        res = asyncio.run(screener.arb_spread_history(
            symbol="BTC", long="binance", short="bybit",
            tf="5s", from_ts=now - 30, to_ts=now,
        ))
    assert len(res["candles"]) == 1
    c = res["candles"][0]
    for k in ("t", "in_o", "in_h", "in_l", "in_c",
              "out_o", "out_h", "out_l", "out_c", "n"):
        assert k in c
    assert c["in_o"] == 0.05
    assert c["n"] == 12
