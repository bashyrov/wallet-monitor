"""
Shared fixtures for all tests.
Uses in-memory SQLite so tests never touch the real database.
"""
import os
import pytest

# Enable the dev-only debug paths: password-reset / email-verify return
# the raw token in the response body so tests can confirm. Production
# never sets this.
os.environ.setdefault("AVALANT_AUTH_DEV_EXPOSE_TOKEN", "1")
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ── In-memory test database ───────────────────────────────────────────────────
TEST_DB_URL = "sqlite://"

_engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,  # all connections share the same in-memory DB
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

# Monkey-patch the prod engine / SessionLocal BEFORE any service imports
# them. Services like admin_settings call `SessionLocal()` directly (not via
# the get_db dep), so without this they hit a separate in-memory DB where
# `Base.metadata.create_all` never ran — tests error with `no such table`.
#
# Safe to do at import time because pytest loads conftest.py first; services
# haven't captured the old binding yet.
import backend.db.base as _db_base  # noqa: E402
_db_base.engine = _engine
_db_base.SessionLocal = _Session


@pytest.fixture(scope="session", autouse=True)
def _create_tables():
    """Create ORM tables once per test session (no Alembic needed)."""
    from backend.db import models  # registers all ORM models
    from backend.db.base import Base
    Base.metadata.create_all(bind=_engine)
    yield
    Base.metadata.drop_all(bind=_engine)


def _seed_minimal_plans(session) -> None:
    """Insert the rows the prod alembic seed creates: a Free plan + the
    standard paid tiers + 4 billing periods. Without these `wallet_quota
    .enforce_for_user` raises `RuntimeError: No active free plan in DB`
    on every /me hit, blocking ~92 tests.

    Mirrors the data shape of `q3r4s5t6u7v8_pricing_promos_popups` but
    keeps it minimal — just enough that get_user_plan / get_free_plan
    return something."""
    from backend.db.models import Plan, BillingPeriod
    if session.query(Plan).first() is not None:
        return  # already seeded this test

    session.add_all([
        Plan(slug="free", name="Free",
             price_usd_monthly=0, price_usd_annual=0,
             portfolio_limit=5, portfolio_limit_grace=5,
             exchange_keys_per_venue=1, trade_delay_ms=500,
             features={}, is_free=True, is_active=True, sort_order=0),
        Plan(slug="screener", name="Screener-only",
             price_usd_monthly=45, price_usd_annual=450,
             portfolio_limit=0, portfolio_limit_grace=0,
             exchange_keys_per_venue=3, trade_delay_ms=0,
             features={}, is_free=False, is_active=True, sort_order=1,
             has_portfolio=False, is_subscription=True),
        Plan(slug="full", name="Full",
             price_usd_monthly=55, price_usd_annual=550,
             portfolio_limit=30, portfolio_limit_grace=30,
             exchange_keys_per_venue=3, trade_delay_ms=0,
             features={}, is_free=False, is_active=True, sort_order=2,
             has_portfolio=True, is_subscription=True),
        Plan(slug="unlim", name="Unlimited",
             price_usd_monthly=0, price_usd_annual=0,
             portfolio_limit=-1, portfolio_limit_grace=-1,
             exchange_keys_per_venue=-1, trade_delay_ms=0,
             features={}, is_free=False, is_active=True, sort_order=99,
             has_portfolio=True, is_subscription=True, is_admin_only=True),
    ])
    session.add_all([
        BillingPeriod(slug="scout",    label="1 month",   months=1,  discount_pct=0,  sort_order=1, is_active=True),
        BillingPeriod(slug="operator", label="3 months",  months=3,  discount_pct=10, sort_order=2, is_active=True),
        BillingPeriod(slug="season",   label="6 months",  months=6,  discount_pct=18, sort_order=3, is_active=True),
        BillingPeriod(slug="desk",     label="12 months", months=12, discount_pct=25, sort_order=4, is_active=True),
    ])
    session.commit()


@pytest.fixture(autouse=True)
def _clean_tables():
    """Wipe every table before each test for full isolation, then re-seed
    the minimal plan + billing-period rows that prod migrations create."""
    yield
    session = _Session()
    try:
        from backend.db.base import Base
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    finally:
        session.close()

    # Reset in-memory rate-limiter state
    from backend.api.v1.auth import _login_attempts
    _login_attempts.clear()

    # Reset admin_settings TTL cache so config a previous test wrote
    # doesn't leak forward (e.g. lowering referral_min_payout_usd).
    try:
        from backend.services import admin_settings
        admin_settings._cache.clear()
    except Exception:
        pass

    # Reset plan_service cache too — same hazard with stale plan rows.
    try:
        from backend.services import plan_service
        if hasattr(plan_service, "invalidate_plan_cache"):
            plan_service.invalidate_plan_cache()
    except Exception:
        pass

    # Reset rate-limit buckets so tests creating many wallets don't
    # bump into wallets_create=30/h or admin_write=60/min mid-suite.
    # Tests share IP 127.0.0.1 — without this, every test after the
    # 30th wallet creation gets 429.
    try:
        from backend.services import rate_limit
        for b in rate_limit._BUCKETS.values():
            b._attempts.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _stub_exchange_validate_key(monkeypatch):
    """Wallet-create calls `adapter.validate_key(creds)` against the
    live exchange. That's network-dependent (Binance 451 on CI, MEXC
    500s, etc) and not what most tests are exercising. Stub every known
    adapter's validate_key to report a clean key so the route runs
    through to create_wallet() and the test can assert persistence,
    display masking, plan limits, etc.

    Lifted out of test_wallets.py so portfolio + provider tests get
    the same protection."""
    from unittest.mock import AsyncMock
    from backend.services import trade_adapters
    ok = {"can_read": True, "can_trade": True, "error": None}
    for name, adapter in (trade_adapters.ADAPTERS or {}).items():
        if hasattr(adapter, "validate_key"):
            monkeypatch.setattr(adapter, "validate_key", AsyncMock(return_value=ok))
        # Same network-isolation rationale: arb-order create now calls
        # adapter.preflight() to validate min-qty / step-size / max-qty
        # before persisting. Stub to {"ok": True} in tests.
        if hasattr(adapter, "preflight"):
            monkeypatch.setattr(adapter, "preflight", AsyncMock(return_value={"ok": True}))
    yield


@pytest.fixture(autouse=True)
def _seed_plans():
    """Re-seed plans BEFORE each test (the _clean_tables yield-after wipes
    the previous test's data). Without this every wallet/portfolio test
    fails on the first /me hit because get_free_plan returns None.

    Also wipe plan_service / admin_settings caches HERE — the prior test's
    `_clean_tables` cleanup runs AFTER the prior test's yield, but the
    cached ORM Plan instances point at sessions that already closed.
    Clearing again here guarantees a clean slate when this test starts."""
    try:
        from backend.services import admin_settings
        admin_settings._cache.clear()
    except Exception:
        pass
    try:
        from backend.services import plan_service
        if hasattr(plan_service, "invalidate_plan_cache"):
            plan_service.invalidate_plan_cache()
    except Exception:
        pass
    session = _Session()
    try:
        _seed_minimal_plans(session)
    finally:
        session.close()
    yield


# ── App / client ──────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    """TestClient with DB overridden to use the in-memory test database."""
    from app import app
    from backend.db.base import get_db

    def _override():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    # Don't use context-manager — avoids running the Alembic lifespan
    yield TestClient(app, raise_server_exceptions=True)
    app.dependency_overrides.clear()


# ── Auth helpers ──────────────────────────────────────────────────────────────
def _register(client, username="alice", email="alice@test.com", password="password123"):
    r = client.post("/api/auth/register", json={
        "username": username, "email": email, "password": password,
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["access_token"]


@pytest.fixture
def token(client):
    return _register(client)


@pytest.fixture
def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_token(client):
    """Register a user, then flip is_admin via direct DB update.

    Production grants admin only through manual SQL on the host — there
    is no API path. The fixture mirrors that contract exactly.
    """
    token = _register(client, "admin", "admin@test.com", "adminpass")
    from backend.db.models import User
    session = _Session()
    try:
        u = session.query(User).filter(User.username == "admin").first()
        u.is_admin = True
        u.plan = "unlim"
        session.commit()
    finally:
        session.close()
    return token


@pytest.fixture
def admin_auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}
