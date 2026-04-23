"""Admin endpoints: stats, user list, toggle admin."""
import pytest
from tests.conftest import _register


def _second_token(client):
    return _register(client, "regular", "regular@test.com", "pass1234")


# ── Access control ────────────────────────────────────────────────────────────

def test_stats_requires_auth(client):
    assert client.get("/api/admin/stats").status_code == 401


def test_stats_requires_admin(client):
    # First user = admin; second user = not admin
    _register(client, "admin", "admin@test.com", "pass1234")
    t = _register(client, "user", "user@test.com", "pass1234")
    r = client.get("/api/admin/stats", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 403


def test_users_list_requires_admin(client):
    _register(client, "admin", "admin@test.com", "pass1234")
    t = _register(client, "user", "user@test.com", "pass1234")
    r = client.get("/api/admin/users", headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 403


# ── Stats ─────────────────────────────────────────────────────────────────────

def test_stats_returns_counts(client, admin_auth):
    data = client.get("/api/admin/stats", headers=admin_auth).json()
    assert "users_count" in data
    assert "wallets_count" in data
    assert "by_type" in data
    assert "recent_users" in data


def test_stats_users_count(client, admin_auth):
    # admin is already registered (1 user)
    _register(client, "extra", "extra@test.com", "pass1234")
    data = client.get("/api/admin/stats", headers=admin_auth).json()
    assert data["users_count"] == 2


def test_stats_wallets_count(client, admin_token, admin_auth):
    client.post("/api/wallets", json={
        "name": "eth wallet",
        "wallet_type": "chain",
        "type_value": "ethereum",
        "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    }, headers=admin_auth)
    data = client.get("/api/admin/stats", headers=admin_auth).json()
    assert data["wallets_count"] >= 1


@pytest.mark.skip(reason="creates a Binance wallet which validates the key against the live exchange; GH runners are geo-blocked (451). Needs provider-level mocking.")
def test_stats_by_type_structure(client, admin_auth):
    # Add wallets of each type so by_type is populated
    client.post("/api/wallets", json={
        "name": "eth wallet", "wallet_type": "chain",
        "type_value": "ethereum", "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    }, headers=admin_auth)
    client.post("/api/wallets", json={
        "name": "bnb wallet", "wallet_type": "exchange",
        "type_value": "binance", "api_key": "k", "api_secret": "s",
    }, headers=admin_auth)
    client.post("/api/wallets", json={
        "name": "hl wallet", "wallet_type": "perpdex",
        "type_value": "hyperliquid", "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    }, headers=admin_auth)
    data = client.get("/api/admin/stats", headers=admin_auth).json()
    by_type = data["by_type"]
    assert "exchange" in by_type
    assert "chain" in by_type
    assert "perpdex" in by_type


# ── Users list ────────────────────────────────────────────────────────────────

def test_users_list_returns_all(client, admin_auth):
    _register(client, "user2", "u2@test.com", "pass1234")
    users = client.get("/api/admin/users", headers=admin_auth).json()
    assert len(users) == 2  # admin + user2


def test_users_list_has_fields(client, admin_auth):
    users = client.get("/api/admin/users", headers=admin_auth).json()
    user = users[0]
    for field in ("id", "username", "email", "is_admin", "created_at"):
        assert field in user, f"Missing field: {field}"


# ── Toggle admin ──────────────────────────────────────────────────────────────
# Tests below reference the legacy PATCH /api/admin/users/{id}/admin
# endpoint that has since been replaced by /block and /plan. They pass
# through a 405 on current main.
# TODO: drop or rewrite against /block + /plan once we decide whether
#       per-flag toggles should return (they were never used in the UI).

@pytest.mark.skip(reason="legacy endpoint /admin removed; use /block + /plan")
def test_toggle_admin(client, admin_auth):
    t = _register(client, "target", "target@test.com", "pass1234")
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {t}"}).json()
    uid = me["id"]

    r = client.patch(f"/api/admin/users/{uid}/admin", headers=admin_auth)
    assert r.status_code == 200
    assert r.json()["is_admin"] is True

    r = client.patch(f"/api/admin/users/{uid}/admin", headers=admin_auth)
    assert r.status_code == 200
    assert r.json()["is_admin"] is False


@pytest.mark.skip(reason="legacy endpoint /admin removed; use /block + /plan")
def test_cannot_toggle_own_admin(client, admin_auth, admin_token):
    me = client.get("/api/auth/me", headers=admin_auth).json()
    r = client.patch(f"/api/admin/users/{me['id']}/admin", headers=admin_auth)
    assert r.status_code == 400
