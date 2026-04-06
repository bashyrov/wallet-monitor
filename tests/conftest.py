"""
Shared fixtures for all tests.
Uses in-memory SQLite so tests never touch the real database.
"""
import pytest
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


@pytest.fixture(scope="session", autouse=True)
def _create_tables():
    """Create ORM tables once per test session (no Alembic needed)."""
    from backend.db import models  # registers all ORM models
    from backend.db.base import Base
    Base.metadata.create_all(bind=_engine)
    yield
    Base.metadata.drop_all(bind=_engine)


@pytest.fixture(autouse=True)
def _clean_tables():
    """Wipe every table before each test for full isolation."""
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
    """First registered user becomes admin automatically."""
    return _register(client, "admin", "admin@test.com", "adminpass")


@pytest.fixture
def admin_auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}
