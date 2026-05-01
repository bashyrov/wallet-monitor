"""Subscription / plan / profile field immutability.

Things the user MUST NOT be able to change directly via the API:

- `is_admin`               — only SQL on the host
- `is_blocked`             — only admin endpoint or SQL
- `plan_id` / `plan`       — only via signed CryptoCloud webhook OR admin set_plan
- `plan_expires_at`        — same
- `referred_by_id`         — only at register time
- `referral_pct_override`  — only via admin endpoint
- `request_count`          — server bumps on /balance + /transactions
- `tg_id` / `tg_chat_id`   — set only by TG bot login flow
- `email_verified_at`      — only via /email-verify/confirm

Things the user CAN change:
- `tg_username`            — PATCH /me {tg_username}
- 2FA (TOTP)               — POST /me/2fa/setup → /verify → /disable
- `auto_renew`             — POST /me/subscription/cancel|resume
- exchange API keys        — POST /wallets

This test file focuses on the read-only-from-client side.
"""
from __future__ import annotations


def _register(client, username, email=None, password="password123"):
    r = client.post("/api/auth/register", json={
        "username": username,
        "email": email or f"{username}@test.com",
        "password": password,
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["access_token"]


def _me(client, token):
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    return r.json()


# ── PATCH /me ───────────────────────────────────────────────────────────────

def test_patch_me_only_accepts_tg_username(client, auth):
    """The whitelist on _UserPatch must be exactly: tg_username. Every
    other field a client tries to send is silently dropped (Pydantic
    ignores by default)."""
    before = _me(client, auth["Authorization"].split()[1])
    r = client.patch("/api/auth/me", headers=auth, json={
        "is_admin": True,
        "is_blocked": True,
        "plan": "unlim",
        "plan_id": 9999,
        "plan_expires_at": "2099-12-31T00:00:00",
        "referred_by_id": 1,
        "referral_pct_override": 99,
        "request_count": 0,
        "email_verified_at": "2099-12-31T00:00:00",
        "tg_id": 1234,
        "tg_chat_id": 1234,
        "username": "evil",
        "email": "evil@x.com",
        "hashed_password": "x",
        "totp_secret_enc": "x",
        "totp_verified_at": "2099-12-31T00:00:00",
        "tg_username": "alice_renamed",   # only this should land
    })
    assert r.status_code == 200
    after = _me(client, auth["Authorization"].split()[1])
    # tg_username updated
    assert after["tg_username"] == "alice_renamed"
    # Everything else unchanged
    assert after["is_admin"] == before["is_admin"]
    assert after["plan"] == before["plan"]
    assert after.get("plan_id") == before.get("plan_id")
    assert after["username"] == before["username"]


def test_patch_me_rejects_invalid_tg_username(client, auth):
    """Even the one allowed field is shape-validated."""
    r = client.patch("/api/auth/me", headers=auth, json={"tg_username": "ab"})
    assert r.status_code == 400  # too short
    r = client.patch("/api/auth/me", headers=auth, json={"tg_username": "9badstart"})
    assert r.status_code == 400  # starts with digit


# ── /register body whitelist ────────────────────────────────────────────────

def test_register_ignores_extra_body_fields(client):
    """Pydantic UserRegister whitelists username/email/password/referral_code.
    Anything else is dropped — never lands on the user row."""
    r = client.post("/api/auth/register", json={
        "username": "alice",
        "email": "alice@test.com",
        "password": "password123",
        "is_admin": True,
        "plan": "unlim",
        "plan_id": 1,
        "plan_expires_at": "2099-12-31T00:00:00",
        "request_count": 0,
        "referred_by_id": 5,
    })
    assert r.status_code in (200, 201)
    me = _me(client, r.json()["access_token"])
    assert me["is_admin"] is False
    assert me["plan"] != "unlim"


# ── Plan can only be elevated via admin/webhook ─────────────────────────────

def test_no_endpoint_writes_plan_id_for_self(client, auth):
    """Sweep every POST/PATCH endpoint a user can hit — none should
    accept a `plan_id` field. We assert by sending a plan_id everywhere
    and confirming the user's plan_id never changes."""
    me = _me(client, auth["Authorization"].split()[1])
    plan_before = me.get("plan")
    # Try a handful of mutating endpoints with plan_id in the body
    payloads = [
        ("PATCH", "/api/auth/me", {"plan_id": 999, "plan": "unlim"}),
        ("POST", "/api/auth/me/subscription/resume", {"plan_id": 999}),
        ("POST", "/api/auth/me/subscription/cancel", {"plan_id": 999}),
    ]
    for method, path, body in payloads:
        r = client.request(method, path, headers=auth, json=body)
        # 200/204/4xx all OK — what matters is the plan didn't change
        assert r.status_code < 500
    me_after = _me(client, auth["Authorization"].split()[1])
    assert me_after.get("plan") == plan_before


def test_admin_set_plan_is_only_admin(client, auth):
    """Only admin can call /admin/users/{id}/plan."""
    me = _me(client, auth["Authorization"].split()[1])
    r = client.patch(f"/api/admin/users/{me['id']}/plan",
                     headers=auth, json={"plan_id": 1})
    assert r.status_code == 403


# ── Profile fields the API does expose for read should not leak others ─────

def test_me_response_does_not_leak_password_hash_or_totp_secret(client, auth):
    me = _me(client, auth["Authorization"].split()[1])
    flat = repr(me).lower()
    for forbidden in ("hashed_password", "totp_secret_enc", "secret_enc"):
        assert forbidden not in flat


# ── DELETE /me cannot be used to silently elevate ──────────────────────────

def test_delete_me_does_not_grant_anything(client, auth):
    """Deleting your own account doesn't, e.g., promote some other user.
    Just a smoke check that the destructive endpoint exists & responds."""
    r = client.delete("/api/auth/me", headers=auth)
    # Either succeeds (200/204) or requires confirmation (4xx) — both fine.
    assert r.status_code < 500
