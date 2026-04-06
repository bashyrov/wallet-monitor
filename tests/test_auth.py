"""Auth: register, login, JWT, rate limiting, /me endpoint."""


# ── Register ──────────────────────────────────────────────────────────────────

def test_register_success(client):
    r = client.post("/api/auth/register", json={
        "username": "user1", "email": "user1@test.com", "password": "pass1234",
    })
    assert r.status_code in (200, 201)
    data = r.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_register_first_user_becomes_admin(client):
    token = client.post("/api/auth/register", json={
        "username": "admin", "email": "admin@test.com", "password": "pass1234",
    }).json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["is_admin"] is True


def test_register_second_user_not_admin(client):
    client.post("/api/auth/register", json={
        "username": "first", "email": "first@test.com", "password": "pass1234",
    })
    token = client.post("/api/auth/register", json={
        "username": "second", "email": "second@test.com", "password": "pass1234",
    }).json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["is_admin"] is False


def test_register_duplicate_username(client):
    client.post("/api/auth/register", json={
        "username": "dup", "email": "dup1@test.com", "password": "pass1234",
    })
    r = client.post("/api/auth/register", json={
        "username": "dup", "email": "dup2@test.com", "password": "pass1234",
    })
    assert r.status_code == 409


def test_register_duplicate_email(client):
    client.post("/api/auth/register", json={
        "username": "user_a", "email": "same@test.com", "password": "pass1234",
    })
    r = client.post("/api/auth/register", json={
        "username": "user_b", "email": "same@test.com", "password": "pass1234",
    })
    assert r.status_code == 409


def test_register_short_password(client):
    r = client.post("/api/auth/register", json={
        "username": "user1", "email": "user1@test.com", "password": "abc",
    })
    assert r.status_code == 422


# ── Login ─────────────────────────────────────────────────────────────────────

def test_login_with_username(client):
    client.post("/api/auth/register", json={
        "username": "alice", "email": "alice@test.com", "password": "secret123",
    })
    r = client.post("/api/auth/login", json={"login": "alice", "password": "secret123"})
    assert r.status_code == 200
    assert "access_token" in r.json()


def test_login_with_email(client):
    client.post("/api/auth/register", json={
        "username": "bob", "email": "bob@test.com", "password": "secret123",
    })
    r = client.post("/api/auth/login", json={"login": "bob@test.com", "password": "secret123"})
    assert r.status_code == 200
    assert "access_token" in r.json()


def test_login_wrong_password(client):
    client.post("/api/auth/register", json={
        "username": "charlie", "email": "charlie@test.com", "password": "correct",
    })
    r = client.post("/api/auth/login", json={"login": "charlie", "password": "wrong"})
    assert r.status_code == 401


def test_login_unknown_user(client):
    r = client.post("/api/auth/login", json={"login": "nobody", "password": "pass"})
    assert r.status_code == 401


# ── /me ───────────────────────────────────────────────────────────────────────

def test_me_returns_user(client, token, auth):
    r = client.get("/api/auth/me", headers=auth)
    assert r.status_code == 200
    data = r.json()
    assert data["username"] == "alice"
    assert data["email"] == "alice@test.com"
    assert "id" in data


def test_me_no_token(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_me_invalid_token(client):
    r = client.get("/api/auth/me", headers={"Authorization": "Bearer invalidtoken"})
    assert r.status_code == 401


# ── Rate limiting ─────────────────────────────────────────────────────────────

def test_rate_limit_triggers(client):
    client.post("/api/auth/register", json={
        "username": "target", "email": "target@test.com", "password": "correct",
    })
    for _ in range(10):
        client.post("/api/auth/login", json={"login": "target", "password": "wrong"})
    r = client.post("/api/auth/login", json={"login": "target", "password": "wrong"})
    assert r.status_code == 429
