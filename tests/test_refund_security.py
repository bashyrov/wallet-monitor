"""Refund + commission reversal — happy paths + edge cases.

The critical contract:

1. Admin can refund any paid payment.
2. Refund is idempotent: replaying it returns the same answer without
   side-effects.
3. Subscription is annulled immediately (plan_expires_at = now,
   auto_renew = False). Replayed success webhooks for a refunded
   payment are refused.
4. Commission reversal honours the partner's payout state:
   a) Earning is unclaimed       → insert -row, partner balance falls.
   b) Earning is in pending payout, new amount ≥ floor → adjust payout.
   c) Earning is in pending payout, new amount < floor → cancel payout,
      every other linked earning returns to the partner's pool.
   d) Earning is in completed payout → log only (money already gone),
      partner's balance goes negative → admin alert fires.
5. Webhook can also trigger a refund (status=refunded / chargeback).
6. Non-admin can't refund (403).
"""
from __future__ import annotations

from decimal import Decimal as D
from datetime import datetime


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


def _ref_me(client, token):
    r = client.get("/api/referrals/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()


def _credit_payment(referee_username: str, amount_paid: float,
                    invoice_id: str | None = None) -> int:
    """Mint a Payment row + run _activate_user. Returns payment.id so
    tests can refer to it later (e.g. for refund)."""
    from backend.db.models import Payment, User
    from backend.services import payment_service
    from tests.conftest import _Session
    from tests.test_referral_security import _ensure_test_plan

    plan_id = _ensure_test_plan()
    s = _Session()
    try:
        u = s.query(User).filter(User.username == referee_username).first()
        invoice = invoice_id or f"inv-refund-{referee_username}-{int(datetime.utcnow().timestamp()*1000)}"
        pmt = Payment(
            user_id=u.id, plan_id=plan_id,
            base_amount_usd=D(str(amount_paid)),
            discount_pct=0,
            final_amount_usd=D(str(amount_paid)),
            status="pending",
            provider="cryptocloud",
            provider_invoice_id=invoice,
        )
        s.add(pmt); s.commit(); s.refresh(pmt)
        pmt.status = "paid"
        pmt.paid_at = datetime.utcnow()
        payment_service._activate_user(s, pmt)
        return pmt.id
    finally:
        s.close()


def _earning_for_payment(payment_id: int):
    from backend.db.models import ReferralEarning
    from tests.conftest import _Session
    s = _Session()
    try:
        return (
            s.query(ReferralEarning)
            .filter(
                ReferralEarning.payment_id == payment_id,
                ReferralEarning.reversal_of_id.is_(None),
            )
            .first()
        )
    finally:
        s.close()


# ── Case A: unclaimed reversal ──────────────────────────────────────────────

def test_refund_reverses_unclaimed_commission(client, admin_auth):
    alice = _register(client, "alice")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])
    pid = _credit_payment("bob", 250)  # → $50 commission

    # Available is $50 before refund
    assert _ref_me(client, alice)["totals"]["available_usd"] == 50.0

    r = client.post(f"/api/admin/payments/{pid}/refund",
                    headers=admin_auth, json={"reason": "user requested"})
    assert r.status_code == 200, r.text
    assert r.json()["referral_action"] == "from_unclaimed"

    # Available drops to $0 (positive original + negative sibling = 0)
    after = _ref_me(client, alice)
    assert after["totals"]["available_usd"] == 0.0
    # Earned net is also $0 (gross $50 + reversal -$50)
    assert after["totals"]["earned_usd"] == 0.0


# ── Case B: pending payout adjusted ─────────────────────────────────────────

def _setup_referrals(client, ref_token, names_amounts):
    """Register N referees against `ref_token`'s code, then credit each.
    Returns list of payment_ids."""
    code = _ref_me(client, ref_token)["code"]
    pids = []
    for name, amt in names_amounts:
        _register(client, name, referral_code=code)
        pids.append(_credit_payment(name, amt))
    return pids


def test_refund_pending_payout_adjusted(client, admin_auth):
    alice = _register(client, "alice")
    pids = _setup_referrals(client, alice, [
        ("oleg", 250),     # $50
        ("nastya", 250),   # $50
        ("egor", 250),     # $50
        ("kostya", 250),   # $50
    ])
    # Total available: $200
    assert _ref_me(client, alice)["totals"]["available_usd"] == 200.0

    # Alice requests payout — claims all 4 earnings, payout = $200
    r1 = client.post("/api/referrals/me/payout",
                     headers={"Authorization": f"Bearer {alice}"},
                     json={"address": "T" + "A" * 33})
    assert r1.status_code == 201
    payout_id = r1.json()["id"]

    # Admin refunds Oleg's $250 payment.
    r2 = client.post(f"/api/admin/payments/{pids[0]}/refund",
                     headers=admin_auth, json={"reason": "test"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["referral_action"] == "from_pending_adjusted"

    # Pending payout reduced from $200 to $150 (still above floor)
    payouts = _ref_me(client, alice)["payouts"]
    p = next(x for x in payouts if x["id"] == payout_id)
    assert p["status"] == "pending"
    assert p["amount_usd"] == 150.0

    # Available stays at $0 — Oleg's earning is back in the pool but
    # offset by the reversal sibling.
    assert _ref_me(client, alice)["totals"]["available_usd"] == 0.0


# ── Case C: pending payout cancelled (would drop below floor) ───────────────

def test_refund_cancels_pending_payout_when_below_floor(client, admin_auth):
    """Alice has 3 referees × $250 = $150 pending. Refund one $250 →
    payout would become $100, exactly the floor → still adjusted, not
    cancelled. Refund a $300-payment one → would drop below."""
    alice = _register(client, "alice")
    pids = _setup_referrals(client, alice, [
        ("oleg", 250),     # $50
        ("nastya", 250),   # $50
        ("egor", 600),     # $120 → makes the math work for "below floor"
    ])
    # Total: $220
    r1 = client.post("/api/referrals/me/payout",
                     headers={"Authorization": f"Bearer {alice}"},
                     json={"address": "T" + "A" * 33})
    assert r1.status_code == 201

    # Refund Egor → would reduce $220 - $120 = $100 (above floor of $100)
    # → adjusted. Test the adjusted path on the boundary.
    r2 = client.post(f"/api/admin/payments/{pids[2]}/refund",
                     headers=admin_auth, json={})
    assert r2.json()["referral_action"] == "from_pending_adjusted"

    # Now refund Oleg → would reduce $100 - $50 = $50 (below floor)
    # → cancelled.
    r3 = client.post(f"/api/admin/payments/{pids[0]}/refund",
                     headers=admin_auth, json={})
    assert r3.status_code == 200
    assert r3.json()["referral_action"] == "from_pending_cancelled"

    # The cancelled payout: nastya's $50 earning returns to pool (claim=null)
    # Oleg's earning: original is reversed, sibling -$50, net 0 in pool
    # Egor's earning: marked reversed BEFORE this refund, sibling -$120 in pool
    # Wait — actually egor was reversed in step 2 → his original is unclaimed
    # (payout adjusted unlinks him), reversal sibling = -$120 → net 0
    # Available = nastya $50 + (oleg net 0) + (egor net 0) = $50
    avail = _ref_me(client, alice)["totals"]["available_usd"]
    assert avail == 50.0

    # The payout is now in cancelled state
    payouts = _ref_me(client, alice)["payouts"]
    cancelled = [p for p in payouts if p["status"] == "cancelled"]
    assert len(cancelled) >= 1


# ── Case D: completed payout — partner already withdrew ─────────────────────

def test_refund_after_completed_payout_logs_only(client, admin_auth):
    alice = _register(client, "alice")
    pids = _setup_referrals(client, alice, [
        ("bob", 700),  # → $140
    ])
    # Alice withdraws + admin marks completed
    r1 = client.post("/api/referrals/me/payout",
                     headers={"Authorization": f"Bearer {alice}"},
                     json={"address": "T" + "A" * 33})
    payout_id = r1.json()["id"]
    rc = client.post(f"/api/admin/referrals/payouts/{payout_id}/complete",
                     headers=admin_auth, json={})
    assert rc.status_code == 200

    # Now the refund happens — money has already left to alice
    r2 = client.post(f"/api/admin/payments/{pids[0]}/refund",
                     headers=admin_auth, json={"reason": "post-payout refund"})
    assert r2.status_code == 200
    assert r2.json()["referral_action"] == "from_completed_logged_only"

    # Available is now NEGATIVE — alice has a $140 debt
    me = _ref_me(client, alice)
    assert me["totals"]["available_usd"] == -140.0

    # The completed payout is unchanged (status still completed, amount $140)
    paid = [p for p in me["payouts"] if p["status"] == "completed"]
    assert paid[0]["amount_usd"] == 140.0


# ── Idempotency ─────────────────────────────────────────────────────────────

def test_refund_is_idempotent(client, admin_auth):
    alice = _register(client, "alice")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])
    pid = _credit_payment("bob", 250)

    r1 = client.post(f"/api/admin/payments/{pid}/refund",
                     headers=admin_auth, json={})
    assert r1.status_code == 200

    # Second call: 409 (already refunded)
    r2 = client.post(f"/api/admin/payments/{pid}/refund",
                     headers=admin_auth, json={})
    assert r2.status_code == 409

    # Available unchanged ($0)
    assert _ref_me(client, alice)["totals"]["available_usd"] == 0.0


def test_refund_annuls_subscription(client, admin_auth):
    alice = _register(client, "alice")
    pid = _credit_payment("alice", 250)

    me_before = client.get("/api/auth/me",
                           headers={"Authorization": f"Bearer {alice}"}).json()
    assert me_before.get("plan") not in (None, "basic", "free")  # paid plan

    client.post(f"/api/admin/payments/{pid}/refund",
                headers=admin_auth, json={})

    # Bust plan cache then re-read /me — alice should be on free tier
    from backend.services import plan_service
    plan_service.invalidate_plan_cache()
    me_after = client.get("/api/auth/me",
                          headers={"Authorization": f"Bearer {alice}"}).json()
    # plan_expires_at is set to now, so user is "expired" — back to free
    assert me_after.get("auto_renew") is False


def test_refunded_payment_cannot_be_re_activated_by_webhook(client, admin_auth, monkeypatch):
    """Even if CryptoCloud sends a stale 'paid' webhook for an invoice
    we already refunded, we must not re-grant the plan."""
    from settings import settings
    monkeypatch.setattr(settings, "CRYPTOCLOUD_WEBHOOK_SECRET", "fake-secret")
    from jose import jwt as _jwt

    alice = _register(client, "alice")
    pid = _credit_payment("alice", 250, invoice_id="inv-replay-after-refund")

    # Refund first
    client.post(f"/api/admin/payments/{pid}/refund",
                headers=admin_auth, json={})

    # Now a stale 'paid' webhook for the same invoice
    token = _jwt.encode({"invoice_id": "inv-replay-after-refund"},
                        "fake-secret", algorithm="HS256")
    r = client.post("/api/payments/cryptocloud/webhook", json={
        "status": "success", "invoice_id": "inv-replay-after-refund", "token": token,
    })
    assert r.status_code == 200

    # Payment is still refunded
    from backend.db.models import Payment
    from tests.conftest import _Session
    s = _Session()
    try:
        p = s.query(Payment).filter(Payment.id == pid).first()
        assert p.status == "refunded"
    finally:
        s.close()


# ── Webhook-initiated refund ────────────────────────────────────────────────

def test_webhook_can_initiate_refund(client, monkeypatch):
    from settings import settings
    monkeypatch.setattr(settings, "CRYPTOCLOUD_WEBHOOK_SECRET", "fake-secret")
    from jose import jwt as _jwt

    alice = _register(client, "alice")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])
    pid = _credit_payment("bob", 250, invoice_id="inv-webhook-refund")

    token = _jwt.encode({"invoice_id": "inv-webhook-refund"},
                        "fake-secret", algorithm="HS256")
    r = client.post("/api/payments/cryptocloud/webhook", json={
        "status": "refunded", "invoice_id": "inv-webhook-refund", "token": token,
    })
    assert r.status_code == 200

    # Payment is refunded + commission reversed
    from backend.db.models import Payment
    from tests.conftest import _Session
    s = _Session()
    try:
        p = s.query(Payment).filter(Payment.id == pid).first()
        assert p.status == "refunded"
    finally:
        s.close()
    assert _ref_me(client, alice)["totals"]["available_usd"] == 0.0


# ── Authorisation ──────────────────────────────────────────────────────────

def test_user_cannot_refund_payments(client, auth):
    r = client.post("/api/admin/payments/1/refund", headers=auth, json={})
    assert r.status_code == 403


def test_unauthenticated_cannot_refund(client):
    r = client.post("/api/admin/payments/1/refund", json={})
    assert r.status_code in (401, 403)


# ── Admin payment list / detail ────────────────────────────────────────────

def test_admin_can_list_and_search_payments(client, admin_auth):
    alice = _register(client, "alice", email="alice@somecorp.io")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])
    _credit_payment("bob", 250)

    # Search by alice's email — alice has no payments though, so result empty
    r = client.get("/api/admin/payments?q=somecorp", headers=admin_auth)
    assert r.status_code == 200
    # Now search for bob's email — should match
    r2 = client.get("/api/admin/payments?q=bob", headers=admin_auth)
    assert r2.status_code == 200
    assert any(p["email"] == "bob@test.com" for p in r2.json()["payments"])


def test_admin_payment_detail_includes_referral_preview(client, admin_auth):
    alice = _register(client, "alice")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])
    pid = _credit_payment("bob", 250)

    r = client.get(f"/api/admin/payments/{pid}", headers=admin_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["referral"] is not None
    assert body["referral"]["referrer"]["username"] == "alice"
    # Preview tells us this is unclaimed
    assert body["refund_preview"]["action"] == "from_unclaimed"


def test_admin_payment_detail_preview_signals_already_withdrawn(client, admin_auth):
    """When the partner already cashed out, the preview must surface the
    "completed payout" warning so the admin sees the consequences."""
    alice = _register(client, "alice")
    _register(client, "bob", referral_code=_ref_me(client, alice)["code"])
    pid = _credit_payment("bob", 700)
    # Alice withdraws + admin completes
    r1 = client.post("/api/referrals/me/payout",
                     headers={"Authorization": f"Bearer {alice}"},
                     json={"address": "T" + "A" * 33})
    payout_id = r1.json()["id"]
    client.post(f"/api/admin/referrals/payouts/{payout_id}/complete",
                headers=admin_auth, json={})

    r = client.get(f"/api/admin/payments/{pid}", headers=admin_auth)
    body = r.json()
    assert body["refund_preview"]["action"] == "from_completed_logged_only"
    assert body["refund_preview"].get("warning")
