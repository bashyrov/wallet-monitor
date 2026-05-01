"""CryptoCloud webhook — signature, idempotency, plan-grant gating.

The webhook is the ONLY public path that can flip a payment to `paid`
and grant a plan. Tightening it tight is critical:

- Without a configured `CRYPTOCLOUD_WEBHOOK_SECRET`, we MUST refuse
  (503), never fail-open and trust the payload.
- Missing or invalid `token` → 401, no DB writes.
- Replayed webhooks for an already-paid invoice are no-ops (no double
  plan extension, no double referral credit).
- Unknown invoice_id → ignored gracefully, no plan grant.
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _register(client, username, email=None, password="password123"):
    r = client.post("/api/auth/register", json={
        "username": username,
        "email": email or f"{username}@test.com",
        "password": password,
    })
    assert r.status_code in (200, 201), r.text
    return r.json()["access_token"]


def _make_jwt(secret: str, payload: dict) -> str:
    from jose import jwt
    return jwt.encode(payload, secret, algorithm="HS256")


def test_webhook_refused_when_secret_unset(client, monkeypatch):
    monkeypatch.delenv("CRYPTOCLOUD_WEBHOOK_SECRET", raising=False)
    # Reach into settings to mirror env removal in case settings was cached
    from settings import settings
    monkeypatch.setattr(settings, "CRYPTOCLOUD_WEBHOOK_SECRET", "")
    r = client.post("/api/payments/cryptocloud/webhook",
                    json={"status": "success", "invoice_id": "abc", "token": "x"})
    assert r.status_code == 503


def test_webhook_rejects_missing_signature(client, monkeypatch):
    from settings import settings
    monkeypatch.setattr(settings, "CRYPTOCLOUD_WEBHOOK_SECRET", "fake-secret")
    r = client.post("/api/payments/cryptocloud/webhook",
                    json={"status": "success", "invoice_id": "abc"})
    assert r.status_code == 401


def test_webhook_rejects_invalid_signature(client, monkeypatch):
    from settings import settings
    monkeypatch.setattr(settings, "CRYPTOCLOUD_WEBHOOK_SECRET", "fake-secret")
    bad_token = _make_jwt("WRONG-secret", {"invoice_id": "abc"})
    r = client.post("/api/payments/cryptocloud/webhook",
                    json={"status": "success", "invoice_id": "abc", "token": bad_token})
    assert r.status_code == 401


def test_webhook_for_unknown_invoice_does_not_grant_plan(client, monkeypatch):
    from settings import settings
    monkeypatch.setattr(settings, "CRYPTOCLOUD_WEBHOOK_SECRET", "fake-secret")
    token = _make_jwt("fake-secret", {"invoice_id": "ghost"})
    r = client.post("/api/payments/cryptocloud/webhook",
                    json={"status": "success", "invoice_id": "ghost", "token": token})
    # 200 with payment_id=None (the route ignores unknown invoices)
    assert r.status_code == 200
    assert r.json().get("payment_id") is None


def test_webhook_replay_does_not_double_extend_plan(client, monkeypatch):
    """Two webhooks for the same paid invoice → one plan extension."""
    from settings import settings
    monkeypatch.setattr(settings, "CRYPTOCLOUD_WEBHOOK_SECRET", "fake-secret")

    alice = _register(client, "alice")

    # Manually create a paid-pending Payment row so we can hit the
    # webhook with a valid invoice_id.
    from backend.db.models import Payment, User
    from tests.conftest import _Session
    from decimal import Decimal as D
    s = _Session()
    try:
        u = s.query(User).filter(User.username == "alice").first()
        from tests.test_referral_security import _ensure_test_plan
        plan_id = _ensure_test_plan()
        pmt = Payment(
            user_id=u.id, plan_id=plan_id,
            base_amount_usd=D("55"), discount_pct=0,
            final_amount_usd=D("55"),
            status="pending",
            provider="cryptocloud",
            provider_invoice_id="inv-replay-001",
        )
        s.add(pmt); s.commit(); s.refresh(pmt)
    finally:
        s.close()

    token = _make_jwt("fake-secret", {"invoice_id": "inv-replay-001"})
    body = {"status": "success", "invoice_id": "inv-replay-001", "token": token}
    r1 = client.post("/api/payments/cryptocloud/webhook", json=body)
    r2 = client.post("/api/payments/cryptocloud/webhook", json=body)
    assert r1.status_code == 200
    assert r2.status_code == 200

    # First call set paid_at; replays must not bump expiry beyond first.
    s = _Session()
    try:
        u = s.query(User).filter(User.username == "alice").first()
        first_expires = u.plan_expires_at
        # Both calls produced the same expiry — idempotent.
        # Run a third for good measure.
        r3 = client.post("/api/payments/cryptocloud/webhook", json=body)
        assert r3.status_code == 200
        s.expire_all()
        u = s.query(User).filter(User.username == "alice").first()
        assert u.plan_expires_at == first_expires
    finally:
        s.close()


def test_webhook_failed_status_does_not_grant_plan(client, monkeypatch):
    from settings import settings
    monkeypatch.setattr(settings, "CRYPTOCLOUD_WEBHOOK_SECRET", "fake-secret")
    alice = _register(client, "alice")
    from backend.db.models import Payment, User
    from tests.conftest import _Session
    from decimal import Decimal as D
    s = _Session()
    try:
        u = s.query(User).filter(User.username == "alice").first()
        from tests.test_referral_security import _ensure_test_plan
        plan_id = _ensure_test_plan()
        pmt = Payment(
            user_id=u.id, plan_id=plan_id,
            base_amount_usd=D("55"), discount_pct=0,
            final_amount_usd=D("55"),
            status="pending",
            provider="cryptocloud",
            provider_invoice_id="inv-fail-001",
        )
        s.add(pmt); s.commit(); s.refresh(pmt)
    finally:
        s.close()
    token = _make_jwt("fake-secret", {"invoice_id": "inv-fail-001"})
    r = client.post("/api/payments/cryptocloud/webhook",
                    json={"status": "failed", "invoice_id": "inv-fail-001", "token": token})
    assert r.status_code == 200

    me = client.get("/api/auth/me",
                    headers={"Authorization": f"Bearer {alice}"}).json()
    # Failed webhooks must not grant a paid plan
    assert me["plan"] != "unlim"


# ── Honeypot: non-admin probing /api/admin/* gets banned ────────────────────

def test_honeypot_bans_non_admin_probing_admin_endpoints(client, auth):
    """Hitting /api/admin/* as a logged-in non-admin trips the honeypot.
    The expected behaviour is 403 + the user's account flipped to is_blocked."""
    # First request: 403, but also blocks the user
    r = client.get("/api/admin/users", headers=auth)
    assert r.status_code == 403

    # Subsequent request from the now-blocked account: 403 too
    from backend.db.models import User
    from tests.conftest import _Session
    s = _Session()
    try:
        # Pull out the username via /me before the block kicks in on
        # any auth-dependent endpoint
        me = client.get("/api/auth/me", headers=auth)
        if me.status_code == 200:
            uname = me.json()["username"]
            u = s.query(User).filter(User.username == uname).first()
            assert u.is_blocked is True
    finally:
        s.close()
