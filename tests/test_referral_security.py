"""Referral system — happy paths + abuse paths.

The contract being protected:

1. **Referral code is immutable** once minted (auto-generated at register
   or first /referrals/me hit). No API path overwrites or rotates it.
2. **`referred_by_id` is set ONCE at register** and is then immutable —
   even if the user later registers again, sends a PATCH, or tries any
   other endpoint. Self-referral is blocked at register time.
3. **Earnings are server-only**: there is no API path to insert,
   delete, or amend a `referral_earnings` row. The only writer is
   `payment_service._activate_user`, which itself runs only after a
   signature-verified webhook.
4. **Available balance is computed from unclaimed earnings, never from
   user-supplied input.** Two simultaneous payout requests can't both
   pass the balance check.
5. **Payout amount is server-computed** = sum of unclaimed earnings.
   Clients submit only the address.
6. **Cancel restores balance**: admin cancel unlinks earnings so the
   user can re-request.
7. **Commission rate is captured at credit time** in
   `referral_earnings.pct`. Admin overrides apply forward only.
"""
from __future__ import annotations

from decimal import Decimal


def _register(client, username, email=None, password="password123",
              referral_code=None):
    body = {
        "username": username,
        "email": email or f"{username}@test.com",
        "password": password,
    }
    if referral_code is not None:
        body["referral_code"] = referral_code
    r = client.post("/api/auth/register", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()["access_token"]


def _me(client, token):
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()


def _ref_me(client, token):
    r = client.get("/api/referrals/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()


def _ensure_test_plan() -> int:
    """Create a minimal Plan row once per session so Payment.plan_id is
    satisfied. Returns the plan id. Idempotent."""
    from backend.db.models import Plan
    from tests.conftest import _Session
    s = _Session()
    try:
        existing = s.query(Plan).filter(Plan.slug == "test-plan").first()
        if existing:
            return existing.id
        p = Plan(
            slug="test-plan",
            name="Test Plan",
            price_usd_monthly=55,
            price_usd_annual=550,
            portfolio_limit=5,
            exchange_keys_per_venue=1,
            has_portfolio=True,
            is_subscription=True,
            is_admin_only=False,
            is_free=False,
            is_active=True,
            sort_order=10,
        )
        s.add(p)
        s.commit()
        return p.id
    finally:
        s.close()


def _credit_payment(referrer_username: str, referee_username: str,
                    amount_paid: float):
    """Simulate a confirmed CryptoCloud webhook by directly invoking the
    same code path: create a Payment row + call _activate_user on it.

    This bypasses HTTP because the test focuses on the bookkeeping
    contract, not the webhook plumbing — there are separate tests for
    the webhook itself."""
    from backend.db.models import Payment, User
    from backend.services import payment_service
    from tests.conftest import _Session
    from datetime import datetime
    from decimal import Decimal as D

    plan_id = _ensure_test_plan()
    session = _Session()
    try:
        referee = session.query(User).filter(User.username == referee_username).first()
        pmt = Payment(
            user_id=referee.id,
            plan_id=plan_id,
            billing_period_id=None,
            base_amount_usd=D(str(amount_paid)),
            discount_pct=0,
            final_amount_usd=D(str(amount_paid)),
            status="pending",
            provider="cryptocloud",
            provider_invoice_id=f"inv-{referee_username}-{int(datetime.utcnow().timestamp()*1000)}",
        )
        session.add(pmt)
        session.commit()
        session.refresh(pmt)
        # Mark paid + run activation (which credits the referral)
        pmt.status = "paid"
        pmt.paid_at = datetime.utcnow()
        payment_service._activate_user(session, pmt)
    finally:
        session.close()


# ── Code minting ────────────────────────────────────────────────────────────

def test_referral_code_is_minted_and_stable(client):
    alice = _register(client, "alice")
    first = _ref_me(client, alice)["code"]
    second = _ref_me(client, alice)["code"]
    assert first
    assert first == second  # idempotent — same code on re-fetch


def test_referral_codes_are_unique_across_users(client):
    a = _register(client, "alice")
    b = _register(client, "bob")
    assert _ref_me(client, a)["code"] != _ref_me(client, b)["code"]


# ── Capture at register ─────────────────────────────────────────────────────

def test_register_captures_valid_referrer(client):
    alice = _register(client, "alice")
    code = _ref_me(client, alice)["code"]
    bob = _register(client, "bob", referral_code=code)
    # Confirm the link landed on the DB row
    from backend.db.models import User
    from tests.conftest import _Session
    s = _Session()
    try:
        b = s.query(User).filter(User.username == "bob").first()
        a = s.query(User).filter(User.username == "alice").first()
        assert b.referred_by_id == a.id
    finally:
        s.close()


def test_register_with_unknown_code_silently_skips(client):
    """Bad / unknown codes don't block registration — the share link
    might be from an old promotion, the user shouldn't be punished."""
    bob = _register(client, "bob", referral_code="ZZZZZZZZ")
    from backend.db.models import User
    from tests.conftest import _Session
    s = _Session()
    try:
        b = s.query(User).filter(User.username == "bob").first()
        assert b.referred_by_id is None
    finally:
        s.close()


def test_register_blocks_self_referral(client):
    """Even if the user somehow learns their own code mid-register, the
    backend must refuse to set referred_by_id = user.id."""
    # We can't easily get a code BEFORE registering, so simulate by
    # manually creating Alice, fetching her code, then having her
    # re-register under a different name with that code (which works
    # since codes belong to anyone) — but if she re-registers under her
    # own name with her own code, the link must not point to herself.
    alice_token = _register(client, "alice")
    code = _ref_me(client, alice_token)["code"]
    # Self-referral is impossible to express cleanly in the API since
    # username uniqueness blocks the duplicate registration. The
    # service-layer guard nevertheless catches the case by-id at
    # credit-time. Verify the credit guard:
    from backend.db.models import User
    from backend.services import referral_service
    from tests.conftest import _Session
    s = _Session()
    try:
        alice = s.query(User).filter(User.username == "alice").first()
        # Pretend Alice somehow ended up with referred_by_id = alice.id
        alice.referred_by_id = alice.id
        s.commit()
        # Try to credit a fake payment for "Alice referred herself"
        from backend.db.models import Payment
        from datetime import datetime
        plan_id = _ensure_test_plan()
        pmt = Payment(
            user_id=alice.id, plan_id=plan_id,
            base_amount_usd=Decimal("55"), discount_pct=0,
            final_amount_usd=Decimal("55"),
            status="paid", paid_at=datetime.utcnow(),
            provider="cryptocloud",
            provider_invoice_id="inv-self-ref-test",
        )
        s.add(pmt); s.commit(); s.refresh(pmt)
        out = referral_service.credit_commission(
            s, referee=alice, payment=pmt, amount_usd=Decimal("55"),
        )
        assert out is None  # self-referral guard fires
    finally:
        s.close()


def test_referred_by_id_cannot_be_changed_via_api(client):
    """Once Alice referred Bob, no API call lets Bob switch to Charlie's
    code. There simply isn't an endpoint that writes referred_by_id."""
    alice = _register(client, "alice")
    charlie = _register(client, "charlie")
    a_code = _ref_me(client, alice)["code"]
    c_code = _ref_me(client, charlie)["code"]
    bob = _register(client, "bob", referral_code=a_code)

    # 1) PATCH /me with referred_by_id — body field is whitelisted, ignored
    r = client.patch(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {bob}"},
        json={"referred_by_id": 999, "referral_code": "FAKE", "referrer_id": 1},
    )
    # Either 200 (ignored) or 422 (rejected); both are acceptable
    assert r.status_code in (200, 422)

    # 2) Re-register with a different code? Username collides → 409
    r = client.post("/api/auth/register", json={
        "username": "bob", "email": "bob2@test.com", "password": "x" * 8,
        "referral_code": c_code,
    })
    assert r.status_code == 409  # username taken; original link preserved

    # Bob's referrer is still Alice
    from backend.db.models import User
    from tests.conftest import _Session
    s = _Session()
    try:
        b = s.query(User).filter(User.username == "bob").first()
        a = s.query(User).filter(User.username == "alice").first()
        assert b.referred_by_id == a.id
    finally:
        s.close()


# ── Credit ─────────────────────────────────────────────────────────────────

def test_commission_credited_on_paid_invoice(client):
    alice = _register(client, "alice")
    code = _ref_me(client, alice)["code"]
    _register(client, "bob", referral_code=code)

    _credit_payment("alice", "bob", 55.0)

    me = _ref_me(client, alice)
    assert me["totals"]["earned_usd"] == 11.0  # 20% of 55
    assert me["totals"]["available_usd"] == 11.0


def test_commission_uses_final_amount_after_promo(client):
    """If the user paid 39.60 (10% off 44), commission is 20% of 39.60
    not 20% of 44. The webhook activation path passes final_amount_usd."""
    alice = _register(client, "alice")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])
    _credit_payment("alice", "bob", 39.60)
    me = _ref_me(client, alice)
    # 39.60 * 0.20 = 7.92
    assert abs(me["totals"]["earned_usd"] - 7.92) < 0.01


def test_commission_idempotent_on_replayed_webhook(client):
    """Replaying the same payment must not double-credit."""
    alice = _register(client, "alice")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])

    from backend.db.models import Payment, User
    from backend.services import payment_service
    from tests.conftest import _Session
    from decimal import Decimal as D
    from datetime import datetime
    s = _Session()
    try:
        bob = s.query(User).filter(User.username == "bob").first()
        plan_id = _ensure_test_plan()
        pmt = Payment(
            user_id=bob.id, plan_id=plan_id,
            base_amount_usd=D("55"), discount_pct=0,
            final_amount_usd=D("55"),
            status="paid", paid_at=datetime.utcnow(),
            provider="cryptocloud",
            provider_invoice_id="inv-replay-test",
        )
        s.add(pmt); s.commit(); s.refresh(pmt)
        # Run activation twice — same payment row, simulating retried webhook
        payment_service._activate_user(s, pmt)
        payment_service._activate_user(s, pmt)
    finally:
        s.close()
    me = _ref_me(client, alice)
    assert me["totals"]["earned_usd"] == 11.0  # not 22


def test_admin_pct_override_applies_forward_only(client, admin_auth):
    """Override = 25% taking effect today doesn't rewrite yesterday's 20%."""
    alice = _register(client, "alice")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])
    _credit_payment("alice", "bob", 100)  # at default 20% → $20
    # Admin sets Alice's override to 25%
    from backend.db.models import User
    from tests.conftest import _Session
    s = _Session()
    try:
        a = s.query(User).filter(User.username == "alice").first()
        r = client.patch(f"/api/admin/users/{a.id}/referral-pct",
                         json={"pct": 25}, headers=admin_auth)
        assert r.status_code == 200
    finally:
        s.close()
    _credit_payment("alice", "bob", 100)  # at 25% → $25
    me = _ref_me(client, alice)
    # First earning $20 (snapshotted 20%) + second $25 = $45
    assert abs(me["totals"]["earned_usd"] - 45) < 0.01


# ── Payout requests ────────────────────────────────────────────────────────

def test_payout_rejects_invalid_address(client):
    alice = _register(client, "alice")
    r = client.post("/api/referrals/me/payout",
                    headers={"Authorization": f"Bearer {alice}"},
                    json={"address": "not-a-trc20"})
    assert r.status_code == 400
    assert "TRC20" in r.json()["detail"]


def test_payout_rejects_below_minimum(client):
    alice = _register(client, "alice")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])
    _credit_payment("alice", "bob", 50)  # → $10 commission
    r = client.post("/api/referrals/me/payout",
                    headers={"Authorization": f"Bearer {alice}"},
                    json={"address": "T" + "A" * 33})
    assert r.status_code == 400
    assert "100" in r.json()["detail"]


def test_admin_can_lower_min_payout(client, admin_auth):
    """Setting min via admin endpoint takes effect immediately — same
    user who couldn't withdraw $50 before now can after the floor moves."""
    alice = _register(client, "alice")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])
    _credit_payment("alice", "bob", 50)  # → $10 commission

    h = {"Authorization": f"Bearer {alice}"}
    addr = "T" + "A" * 33
    # Default $100 — rejected.
    r1 = client.post("/api/referrals/me/payout", headers=h, json={"address": addr})
    assert r1.status_code == 400

    # Admin moves the floor to $5.
    r_admin = client.patch("/api/admin/screener-config",
                           headers=admin_auth,
                           json={"referral_min_payout_usd": 5.0})
    assert r_admin.status_code == 200
    # Bust the admin_settings cache so the new value lands immediately.
    from backend.services import admin_settings
    admin_settings._cache.clear()

    # Same user, same balance — but now they're above the new floor.
    r2 = client.post("/api/referrals/me/payout", headers=h, json={"address": addr})
    assert r2.status_code == 201
    assert abs(r2.json()["amount_usd"] - 10.0) < 0.01


def test_user_cannot_set_min_payout(client, auth):
    r = client.patch("/api/admin/screener-config",
                     headers=auth,
                     json={"referral_min_payout_usd": 1.0})
    assert r.status_code == 403


def test_min_payout_clamped_to_safe_range(client, admin_auth):
    """Admin can't typo a 0 or a billion and brick the system."""
    from backend.services import admin_settings

    # Below floor — clamped to 1
    client.patch("/api/admin/screener-config",
                 headers=admin_auth,
                 json={"referral_min_payout_usd": -50})
    admin_settings._cache.clear()
    assert admin_settings.get_referral_min_payout_usd() == 1.0

    # Above ceiling — clamped to 10_000
    client.patch("/api/admin/screener-config",
                 headers=admin_auth,
                 json={"referral_min_payout_usd": 1_000_000})
    admin_settings._cache.clear()
    assert admin_settings.get_referral_min_payout_usd() == 10000.0


def test_payout_claims_all_unclaimed_earnings(client):
    alice = _register(client, "alice")
    for name in ("oleg", "nastya", "egor", "kostya"):
        _register(client, name, referral_code=_ref_me(client, alice)["code"])
        _credit_payment("alice", name, 250)  # 4 × 250 = 1000 → 4 × 50 = 200

    r = client.post("/api/referrals/me/payout",
                    headers={"Authorization": f"Bearer {alice}"},
                    json={"address": "T" + "A" * 33})
    assert r.status_code == 201, r.text
    body = r.json()
    assert abs(body["amount_usd"] - 200) < 0.01

    # Available is now zero, and all 4 earnings show claimed=true
    me = _ref_me(client, alice)
    assert me["totals"]["available_usd"] == 0
    assert all(h["claimed"] for h in me["history"])


def test_only_one_pending_payout_at_a_time(client):
    alice = _register(client, "alice")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])
    _credit_payment("alice", "bob", 700)  # → $140

    addr = "T" + "A" * 33
    h = {"Authorization": f"Bearer {alice}"}
    r1 = client.post("/api/referrals/me/payout", headers=h, json={"address": addr})
    assert r1.status_code == 201
    r2 = client.post("/api/referrals/me/payout", headers=h, json={"address": addr})
    assert r2.status_code == 409


def test_admin_cancel_returns_earnings_to_pool(client, admin_auth):
    alice = _register(client, "alice")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])
    _credit_payment("alice", "bob", 700)  # → $140

    h = {"Authorization": f"Bearer {alice}"}
    addr = "T" + "A" * 33
    r1 = client.post("/api/referrals/me/payout", headers=h, json={"address": addr})
    payout_id = r1.json()["id"]

    # Admin cancels
    r2 = client.post(f"/api/admin/referrals/payouts/{payout_id}/cancel",
                     headers=admin_auth, json={"note": "test"})
    assert r2.status_code == 200, r2.text

    # Earnings flow back into available — Alice can request again
    me = _ref_me(client, alice)
    assert abs(me["totals"]["available_usd"] - 140) < 0.01
    assert me["has_pending_payout"] is False

    r3 = client.post("/api/referrals/me/payout", headers=h, json={"address": addr})
    assert r3.status_code == 201


def test_admin_complete_locks_earnings_out_of_available(client, admin_auth):
    alice = _register(client, "alice")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])
    _credit_payment("alice", "bob", 700)
    h = {"Authorization": f"Bearer {alice}"}
    r1 = client.post("/api/referrals/me/payout", headers=h, json={"address": "T" + "A" * 33})
    payout_id = r1.json()["id"]
    client.post(f"/api/admin/referrals/payouts/{payout_id}/complete",
                headers=admin_auth, json={"note": "tx 0xabc"})

    me = _ref_me(client, alice)
    assert me["totals"]["available_usd"] == 0
    assert me["totals"]["paid_usd"] == 140
    # No second request without new earnings
    r2 = client.post("/api/referrals/me/payout", headers=h, json={"address": "T" + "A" * 33})
    assert r2.status_code == 400


def test_admin_payout_detail_lists_linked_earnings(client, admin_auth):
    alice = _register(client, "alice")
    for name in ("oleg", "nastya"):
        _register(client, name, referral_code=_ref_me(client, alice)["code"])
        _credit_payment("alice", name, 250)
    h = {"Authorization": f"Bearer {alice}"}
    r1 = client.post("/api/referrals/me/payout", headers=h, json={"address": "T" + "A" * 33})
    payout_id = r1.json()["id"]

    r = client.get(f"/api/admin/referrals/payouts/{payout_id}", headers=admin_auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["amount_usd"] == 100  # 2 × 50
    assert len(body["earnings"]) == 2
    names = sorted(e["referee_username"] for e in body["earnings"])
    assert names == ["nastya", "oleg"]
    # Sum-check matches amount
    assert abs(body["earnings_sum_check"] - body["amount_usd"]) < 0.01


def test_admin_complete_is_idempotent_409(client, admin_auth):
    alice = _register(client, "alice")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])
    _credit_payment("alice", "bob", 700)
    h = {"Authorization": f"Bearer {alice}"}
    r1 = client.post("/api/referrals/me/payout", headers=h, json={"address": "T" + "A" * 33})
    pid = r1.json()["id"]
    r2 = client.post(f"/api/admin/referrals/payouts/{pid}/complete",
                     headers=admin_auth, json={})
    assert r2.status_code == 200
    r3 = client.post(f"/api/admin/referrals/payouts/{pid}/complete",
                     headers=admin_auth, json={})
    assert r3.status_code == 409


# ── Authorisation ──────────────────────────────────────────────────────────

def test_user_cannot_call_admin_payout_routes(client, auth):
    """Plain user hitting /admin/referrals/* must 403, not 200."""
    r = client.get("/api/admin/referrals/payouts", headers=auth)
    assert r.status_code == 403
    r2 = client.post("/api/admin/referrals/payouts/1/complete", headers=auth, json={})
    assert r2.status_code == 403
    r3 = client.post("/api/admin/referrals/payouts/1/cancel", headers=auth, json={})
    assert r3.status_code == 403


def test_user_cannot_set_their_own_pct_override(client, auth):
    """The pct-override endpoint is admin-only — there's no /me variant."""
    # Find Alice's user_id by reading /me
    me = client.get("/api/auth/me", headers=auth).json()
    r = client.patch(f"/api/admin/users/{me['id']}/referral-pct",
                     headers=auth, json={"pct": 99})
    assert r.status_code == 403


def test_unauthenticated_payout_rejected(client):
    r = client.post("/api/referrals/me/payout", json={"address": "T" + "A" * 33})
    assert r.status_code in (401, 403)
