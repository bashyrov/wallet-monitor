"""Admin grant policy: SQL-only.

After 2026-05, the only path to admin is direct DB update on the host
(`UPDATE users SET is_admin=TRUE WHERE …`). Regression guards:

- /register never produces an admin, regardless of env vars or DB state.
- /api/auth/tg-login never produces an admin (TG-widget bypass closed).
- Once a user is admin (via SQL), the API has no path to demote — so the
  honesty check is "does the API try to elevate". It must not.
"""
from __future__ import annotations


def _me(client, token):
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()


def _register(client, username, email=None, password="password123"):
    r = client.post("/api/auth/register", json={
        "username": username,
        "email": email or f"{username}@test.com",
        "password": password,
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["access_token"]


def test_register_never_grants_admin_on_empty_db(client, monkeypatch):
    """Empty DB + no env vars → first user is still NOT admin."""
    monkeypatch.delenv("INITIAL_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("AVALANT_ALLOW_FIRST_USER_ADMIN", raising=False)
    alice = _register(client, "alice")
    assert _me(client, alice)["is_admin"] is False
    assert _me(client, alice)["plan"] == "basic"


def test_register_ignores_initial_admin_username_env(client, monkeypatch):
    """The legacy INITIAL_ADMIN_USERNAME env var is no longer honoured —
    setting it should NOT grant admin to the matching username."""
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "ops")
    ops = _register(client, "ops")
    assert _me(client, ops)["is_admin"] is False


def test_register_ignores_allow_first_user_admin_env(client, monkeypatch):
    """The legacy dev-only AVALANT_ALLOW_FIRST_USER_ADMIN flag is no longer
    honoured. Empty DB + flag set → still not admin."""
    monkeypatch.setenv("AVALANT_ALLOW_FIRST_USER_ADMIN", "1")
    alice = _register(client, "alice")
    assert _me(client, alice)["is_admin"] is False


def test_admin_only_via_sql_path(client, _create_tables):
    """Direct DB write is the canonical (and only) way to grant admin."""
    token = _register(client, "alice")
    assert _me(client, token)["is_admin"] is False

    # SQL-equivalent — the operations team would run this on the prod box
    from backend.db.models import User
    from tests.conftest import _Session
    session = _Session()
    try:
        u = session.query(User).filter(User.username == "alice").first()
        u.is_admin = True
        session.commit()
    finally:
        session.close()

    assert _me(client, token)["is_admin"] is True


def test_register_body_cannot_set_is_admin(client):
    """Even if the client tampers with the body, is_admin is server-controlled."""
    r = client.post("/api/auth/register", json={
        "username": "evil",
        "email": "evil@x.com",
        "password": "password123",
        "is_admin": True,           # client-controlled, must be ignored
        "plan": "unlim",            # ditto
        "plan_id": 999,             # ditto
    })
    assert r.status_code in (200, 201)
    token = r.json()["access_token"]
    assert _me(client, token)["is_admin"] is False
    assert _me(client, token)["plan"] != "unlim"
