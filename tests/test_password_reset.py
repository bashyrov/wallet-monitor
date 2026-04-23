"""Password reset flow: request → confirm."""
from __future__ import annotations


def _register(client, username, email, password="password123"):
    r = client.post("/api/auth/register", json={
        "username": username, "email": email, "password": password,
    })
    assert r.status_code in (200, 201)
    return r.json()["access_token"]


def test_request_nonexistent_email_returns_generic(client, monkeypatch):
    """Never leak whether an email is registered."""
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    r = client.post("/api/auth/password-reset/request", json={"email": "nobody@none.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # No dev_token for unknown emails
    assert "dev_token" not in body


def test_request_existing_email_returns_dev_token(client, monkeypatch):
    """In dev mode (no mailer configured), response includes the raw token
    so ops can complete the flow manually."""
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    _register(client, "alice", "alice@test.com")

    r = client.post("/api/auth/password-reset/request", json={"email": "alice@test.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "dev_token" in body and len(body["dev_token"]) >= 32


def test_confirm_with_valid_token_changes_password(client, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    _register(client, "alice", "alice@test.com", password="old_password_12")

    req = client.post("/api/auth/password-reset/request", json={"email": "alice@test.com"})
    token = req.json()["dev_token"]

    r = client.post("/api/auth/password-reset/confirm", json={
        "token": token, "new_password": "new_password_xy",
    })
    assert r.status_code == 200

    # Old password no longer works
    r_old = client.post("/api/auth/login", json={
        "login": "alice@test.com", "password": "old_password_12",
    })
    assert r_old.status_code == 401

    # New password works
    r_new = client.post("/api/auth/login", json={
        "login": "alice@test.com", "password": "new_password_xy",
    })
    assert r_new.status_code == 200


def test_confirm_reused_token_rejected(client, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    _register(client, "alice", "alice@test.com", password="old_password_12")

    req = client.post("/api/auth/password-reset/request", json={"email": "alice@test.com"})
    token = req.json()["dev_token"]

    r1 = client.post("/api/auth/password-reset/confirm", json={
        "token": token, "new_password": "new_password_xy",
    })
    assert r1.status_code == 200

    # Second use must fail
    r2 = client.post("/api/auth/password-reset/confirm", json={
        "token": token, "new_password": "another_pw_ab",
    })
    assert r2.status_code == 400


def test_confirm_short_password_rejected(client, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    _register(client, "alice", "alice@test.com")

    token = client.post("/api/auth/password-reset/request",
                        json={"email": "alice@test.com"}).json()["dev_token"]
    r = client.post("/api/auth/password-reset/confirm", json={
        "token": token, "new_password": "short",
    })
    assert r.status_code == 400
