"""INITIAL_ADMIN_USERNAME env-var seeding logic.

Regression guard: prod previously granted admin + unlim to whoever
registered first. That's a race anyone on the internet can win. Now the
admin is named by env var; matching username gets admin, everyone else
stays basic.
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


def test_legacy_first_user_is_admin_when_seed_unset(client, monkeypatch):
    """Back-compat: with no env var set, the first registered user is still
    admin (keeps dev / local flow unchanged)."""
    monkeypatch.delenv("INITIAL_ADMIN_USERNAME", raising=False)
    alice = _register(client, "alice")
    bob = _register(client, "bob")
    assert _me(client, alice)["is_admin"] is True
    assert _me(client, bob)["is_admin"] is False


def test_seeded_username_becomes_admin(client, monkeypatch):
    """With INITIAL_ADMIN_USERNAME set, ONLY that username is admin — even
    a user registered before them stays basic."""
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "ops")

    bad = _register(client, "malicious")
    assert _me(client, bad)["is_admin"] is False
    assert _me(client, bad)["plan"] == "basic"

    ops = _register(client, "ops")
    me_ops = _me(client, ops)
    assert me_ops["is_admin"] is True
    assert me_ops["plan"] == "unlim"


def test_seed_matches_case_insensitive(client, monkeypatch):
    """Usernames are normalised to lowercase, so the seed check must be too."""
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "Ops")
    token = _register(client, "OPS")
    assert _me(client, token)["is_admin"] is True
