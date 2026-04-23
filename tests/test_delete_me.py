"""Self-service account deletion."""
from __future__ import annotations


def _register(client, username, email, password="password123"):
    r = client.post("/api/auth/register", json={
        "username": username, "email": email, "password": password,
    })
    assert r.status_code in (200, 201)
    return r.json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_delete_requires_auth(client):
    r = client.request("DELETE", "/api/auth/me", json={"password": "x"})
    assert r.status_code == 401


def test_delete_wrong_password(client):
    # First user is admin — register a second (regular) user to test delete
    _register(client, "admin", "admin@test.com")
    t = _register(client, "alice", "alice@test.com", password="password123")
    r = client.request("DELETE", "/api/auth/me", headers=_auth(t),
                       json={"password": "wrong"})
    assert r.status_code == 401


def test_admin_cannot_self_delete(client):
    t = _register(client, "admin", "admin@test.com", password="password123")
    r = client.request("DELETE", "/api/auth/me", headers=_auth(t),
                       json={"password": "password123"})
    assert r.status_code == 400


def test_delete_ok_and_token_stops_working(client):
    _register(client, "admin", "admin@test.com")
    t = _register(client, "alice", "alice@test.com", password="password123")

    r = client.request("DELETE", "/api/auth/me", headers=_auth(t),
                       json={"password": "password123"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    # Token no longer resolves to a user — /auth/me should 401
    me = client.get("/api/auth/me", headers=_auth(t))
    assert me.status_code == 401
