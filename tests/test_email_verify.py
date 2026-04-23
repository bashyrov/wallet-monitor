"""Email verification flow."""
from __future__ import annotations


def _register(client, username, email, password="password123"):
    r = client.post("/api/auth/register", json={
        "username": username, "email": email, "password": password,
    })
    assert r.status_code in (200, 201)
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_new_user_email_is_unverified(client):
    t = _register(client, "alice", "alice@test.com")
    me = client.get("/api/auth/me", headers=_auth(t)).json()
    assert me["email_verified_at"] is None


def test_request_dev_token_and_confirm(client, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    t = _register(client, "alice", "alice@test.com")

    r = client.post("/api/auth/email-verify/request", headers=_auth(t))
    assert r.status_code == 200
    body = r.json()
    assert "dev_token" in body
    token = body["dev_token"]

    r2 = client.post("/api/auth/email-verify/confirm", json={"token": token})
    assert r2.status_code == 200
    assert r2.json()["status"] == "ok"

    me = client.get("/api/auth/me", headers=_auth(t)).json()
    assert me["email_verified_at"] is not None


def test_request_already_verified(client, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    t = _register(client, "alice", "alice@test.com")
    token = client.post("/api/auth/email-verify/request", headers=_auth(t)).json()["dev_token"]
    client.post("/api/auth/email-verify/confirm", json={"token": token})

    r = client.post("/api/auth/email-verify/request", headers=_auth(t))
    assert r.status_code == 200
    assert r.json().get("already_verified") is True


def test_confirm_invalid_token(client):
    r = client.post("/api/auth/email-verify/confirm", json={"token": "bogus"})
    assert r.status_code == 400


def test_confirm_used_token_rejected(client, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    t = _register(client, "alice", "alice@test.com")
    token = client.post("/api/auth/email-verify/request", headers=_auth(t)).json()["dev_token"]

    r1 = client.post("/api/auth/email-verify/confirm", json={"token": token})
    assert r1.status_code == 200

    r2 = client.post("/api/auth/email-verify/confirm", json={"token": token})
    assert r2.status_code == 400
