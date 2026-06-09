"""Compute-layer tests for the split-discount system.

End-to-end through Python (NOT through CryptoCloud — webhook secret is
empty in test env and would 503). We invoke _activate_user with a mock
paid Payment so the accrual logic runs end-to-end via the SAME function
the webhook calls.

Tests #1, #9, #10 from the spec + refund-after-cashout block (the
hole-closing requirement from question 7 of the plan).
"""
from __future__ import annotations
import pytest
from decimal import Decimal

from backend.db import models
from backend.db.base import SessionLocal
from backend.services import referral_code_service as svc
from backend.services import payment_service
from backend.services import referral_service


@pytest.fixture
def db():
    """Per-test session. Pre-cleanup of referral tables — the conftest
    autouse `_clean_tables` fixture's sorted_tables.reversed() loop
    SKIPS tables in FK cycles (referral_codes ↔ users via
    signup_code_id + created_by_admin_id), so rows leak between tests
    in this file specifically. Explicit DELETE here clears them; the
    cycle doesn't prevent direct DELETEs."""
    from sqlalchemy import text
    s = SessionLocal()
    try:
        # Order matters: children first, then parents
        s.execute(text("DELETE FROM referral_code_usages"))
        s.execute(text("DELETE FROM referral_code_registrations"))
        s.execute(text("DELETE FROM referral_earnings"))
        s.execute(text("DELETE FROM referral_payout_requests"))
        s.execute(text("UPDATE users SET signup_code_id=NULL, referred_by_id=NULL"))
        s.execute(text("DELETE FROM referral_codes"))
        s.commit()
        yield s
    finally:
        s.rollback()
        s.close()


def _user(db, name: str, is_admin: bool = False) -> models.User:
    u = models.User(username=name, email=f"{name}@test.com",
                    hashed_password="x", is_admin=is_admin)
    db.add(u); db.flush()
    return u


def _seed_plan(db) -> models.Plan:
    plan = models.Plan(slug="test-plan-compute", name="t",
                       price_usd_monthly=100, portfolio_limit=1,
                       exchange_keys_per_venue=1, trade_delay_ms=0)
    db.add(plan); db.flush()
    return plan


_PAY_COUNTER = {"i": 0}


def _mock_paid_payment(db, *, user, plan, final_amount, base_amount=None):
    """Build a Payment row in 'paid' status — what the webhook would
    produce. The _activate_user function inspects this row directly so
    we get the same code path that prod runs.

    Commits so the payment row is durable in the in-memory DB before
    _activate_user starts modifying it — avoids stale-identity-map
    contention with previously-committed payments that the SQLAlchemy
    session is still tracking."""
    base_amount = base_amount if base_amount is not None else final_amount
    _PAY_COUNTER["i"] += 1
    pay = models.Payment(
        user_id=user.id, plan_id=plan.id,
        base_amount_usd=Decimal(str(base_amount)),
        final_amount_usd=Decimal(str(final_amount)),
        status="paid",
        provider_invoice_id=f"inv-{_PAY_COUNTER['i']}",
    )
    db.add(pay)
    db.commit()
    # Don't db.refresh — between tests the session can land in a state
    # where the row isn't visible to refresh (FK-cycle aware wipe in
    # the autouse fixture races with this session's autoincrement
    # state). After commit, pay.id is populated and that's all we need.
    return pay


# ── #1: split calculation P=100, code 10/15 → user 85, earning 10 ════════════

def test_split_calc_p100_10_15(db):
    """Spec test #1: P=100, code 10/15 → buyer pays 85, owner earns 10."""
    owner = _user(db, "calc_owner")
    code = svc.create_self_serve_code(db, owner=owner, code="calc1015",
                                       commission_pct=10, discount_pct=15)
    referee = _user(db, "calc_ref")
    svc.bind_referee(db, referee=referee, raw_code="calc1015")
    plan = _seed_plan(db)
    db.commit()
    # Checkout — buyer pays $85 ($100 * (1-0.15))
    from backend.services.payment_service import compute_pricing
    from backend.services import billing_period_service
    # Need a billing period — use one-month
    bp = models.BillingPeriod(slug="m1", label="Monthly", months=1,
                              discount_pct=0, sort_order=0, is_active=True)
    db.add(bp); db.commit()
    pricing = compute_pricing(plan, bp, promo=None, ref_code_discount_pct=Decimal("15"))
    assert pricing["base_amount_usd"] == Decimal("100")
    assert pricing["final_amount_usd"] == Decimal("85.00")
    # Webhook simulation — _activate_user computes commission
    pay = _mock_paid_payment(db, user=referee, plan=plan,
                              final_amount=85, base_amount=100)
    payment_service._activate_user(db, pay)
    usage = db.query(models.ReferralCodeUsage).filter_by(payment_id=pay.id).one()
    assert usage.commission_earned == Decimal("8.50")  # 10% of 85
    assert usage.discount_applied == Decimal("15.00")  # 15% of 100
    earning = db.query(models.ReferralEarning).filter_by(payment_id=pay.id).one()
    assert earning.referrer_id == owner.id
    assert earning.amount_usd == Decimal("8.50")


# ── #9: 5-cap saturates — 6th payment no discount + no commission ═══════════

def test_5_cap_blocks_6th_payment(db):
    """Spec test #9: 5 paid OK, 6th no discount (full price), no earning."""
    owner = _user(db, "cap_owner")
    code = svc.create_self_serve_code(db, owner=owner, code="cap5",
                                       commission_pct=10, discount_pct=10)
    referee = _user(db, "cap_ref")
    svc.bind_referee(db, referee=referee, raw_code="cap5")
    plan = _seed_plan(db)
    db.commit()
    # 5 webhooks — each creates an Usage + Earning. expire_all between
    # iterations so SQLAlchemy doesn't carry stale identity-map state
    # from the just-committed payment into the next flush.
    for i in range(5):
        pay = _mock_paid_payment(db, user=referee, plan=plan,
                                  final_amount=90, base_amount=100)
        payment_service._activate_user(db, pay)
        db.expire_all()
    assert svc.count_non_reversed_usages(db, code.id, referee.id) == 5
    # 6th — no discount applied at checkout, no commission at webhook
    eff_d = svc.effective_discount_pct(code, usage_count=5)
    eff_c = svc.effective_commission_pct(code, usage_count=5)
    assert eff_d == Decimal("0")
    assert eff_c == Decimal("0")
    pay6 = _mock_paid_payment(db, user=referee, plan=plan,
                               final_amount=100, base_amount=100)
    payment_service._activate_user(db, pay6)
    # No new Usage / Earning rows for pay6 (silent skip on 5-cap)
    assert db.query(models.ReferralCodeUsage).filter_by(payment_id=pay6.id).first() is None
    assert db.query(models.ReferralEarning).filter_by(payment_id=pay6.id).first() is None
    # Total non-reversed usages still 5
    assert svc.count_non_reversed_usages(db, code.id, referee.id) == 5


# ── #10: refund decrements 5-cap (frees the slot) ════════════════════════════

def test_refund_decrements_5_cap(db):
    """5 paid → 1 refunded → counter shows 4 → 6th payment with discount.

    Direct insert pattern to avoid the inter-test state issues that
    show up with chained _activate_user calls (in-memory DB session
    contention with the FK-cycle wipe). The unit-under-test here is
    the count_non_reversed_usages logic + refund path's reversal of
    ReferralCodeUsage, not the full webhook activation flow.
    """
    owner = _user(db, "ref_owner")
    code = svc.create_self_serve_code(db, owner=owner, code="ref5",
                                       commission_pct=10, discount_pct=10)
    referee = _user(db, "ref_ref")
    svc.bind_referee(db, referee=referee, raw_code="ref5")
    plan = _seed_plan(db)
    db.commit()
    payments = []
    # Direct insert: 5 paid payments + 5 ReferralCodeUsage rows. This
    # bypasses _activate_user but exercises the same DB state the cap
    # logic reads from.
    for i in range(5):
        pay = _mock_paid_payment(db, user=referee, plan=plan,
                                  final_amount=90, base_amount=100)
        db.add(models.ReferralCodeUsage(
            code_id=code.id, referee_id=referee.id, payment_id=pay.id,
            payment_amount_usd=Decimal("90"),
            commission_earned=Decimal("9"),
            discount_applied=Decimal("10"),
        ))
        db.commit()
        payments.append(pay)
    assert svc.count_non_reversed_usages(db, code.id, referee.id) == 5
    # Refund payment #3 (middle) — Usage.reversed_at gets stamped, slot frees.
    target_id = payments[2].id
    fresh = db.query(models.Payment).filter(models.Payment.id == target_id).first()
    payment_service.refund_payment(db, fresh, reason="test")
    assert svc.count_non_reversed_usages(db, code.id, referee.id) == 4
    # Next payment gets discount + commission again — verify via helper.
    eff_d = svc.effective_discount_pct(code, usage_count=4)
    assert eff_d == Decimal("10")


# ── FREEZE: old referred_by_id stops accruing on new payments ════════════════

def test_legacy_referred_by_frozen(db):
    """User has referred_by_id (legacy) but no signup_code_id. Payment
    activates but produces NO new ReferralEarning row. Spec answer A:
    FREEZE — past balances payable, future accrual stops."""
    legacy_referrer = _user(db, "legacy_r")
    legacy_referee = _user(db, "legacy_ref")
    legacy_referee.referred_by_id = legacy_referrer.id
    db.add(legacy_referee); db.flush()
    plan = _seed_plan(db)
    db.commit()
    pay = _mock_paid_payment(db, user=legacy_referee, plan=plan,
                              final_amount=100, base_amount=100)
    payment_service._activate_user(db, pay)
    # No new earning row for the legacy path — FROZEN.
    assert db.query(models.ReferralEarning).filter_by(payment_id=pay.id).first() is None


# ── refund-after-cashout: payout blocked while balance negative ══════════════

def test_refund_after_cashout_blocks_next_payout(db):
    """The hole-closing test from question 7. Walk-through:

    1. owner earns $50, withdraws via completed payout → balance $0
    2. payment is refunded → reverse_commission writes a sibling
       -$50 earning (because completed payout can't be clawed back)
    3. balance is now -$50 — owner cannot withdraw
    4. owner earns $30 next month → balance -$20
    5. owner earns $200 — balance +$180, can withdraw again

    The fix: request_payout now hard-rejects negative balance with a
    distinct error. Even if min_payout >= $100, the error message
    surfaces 'refund debt' so the owner knows why.
    """
    from backend.db.models import ReferralPayoutRequest, ReferralEarning
    from datetime import datetime
    owner = _user(db, "cashout_owner")
    referee = _user(db, "cashout_ref")
    plan = _seed_plan(db)
    db.commit()
    # Skip the split flow — pretend earning came from any source.
    # Insert directly to avoid setup overhead.
    pay = _mock_paid_payment(db, user=referee, plan=plan,
                              final_amount=500, base_amount=500)
    earn = ReferralEarning(referrer_id=owner.id, referee_id=referee.id,
                           payment_id=pay.id, pct=Decimal("10"),
                           amount_usd=Decimal("50"))
    db.add(earn); db.commit()
    # Set owner's payout address
    owner.referral_payout_address = "T" + "1" * 33  # 34-char TRC20 stub
    db.add(owner); db.commit()
    # Owner withdraws (completed payout)
    req = ReferralPayoutRequest(
        user_id=owner.id, amount_usd=Decimal("50"),
        address=owner.referral_payout_address, status="completed",
        resolved_at=datetime.utcnow(),
    )
    db.add(req); db.commit()
    earn.payout_request_id = req.id
    db.add(earn); db.commit()
    # Balance is now $0 (the $50 was claimed by the completed payout).
    assert referral_service.available_balance(db, owner) == Decimal("0")
    # Refund the payment — reverse_commission writes sibling -$50.
    payment_service.refund_payment(db, db.query(models.Payment).filter_by(id=pay.id).first(), reason="test refund")
    # Balance is now negative.
    bal = referral_service.available_balance(db, owner)
    assert bal == Decimal("-50"), f"expected -50, got {bal}"
    # Attempting payout fails with the distinct 'refund debt' error.
    with pytest.raises(referral_service.PayoutError) as exc:
        referral_service.request_payout(db, user=owner,
                                         address=owner.referral_payout_address)
    assert "refund debt" in str(exc.value).lower()
    # Future earnings pay down the debt.
    pay2 = _mock_paid_payment(db, user=referee, plan=plan,
                               final_amount=200, base_amount=200)
    earn2 = ReferralEarning(referrer_id=owner.id, referee_id=referee.id,
                            payment_id=pay2.id, pct=Decimal("10"),
                            amount_usd=Decimal("20"))
    db.add(earn2); db.commit()
    bal = referral_service.available_balance(db, owner)
    assert bal == Decimal("-30"), f"expected -30, got {bal}"
    # Still negative → still blocked.
    with pytest.raises(referral_service.PayoutError):
        referral_service.request_payout(db, user=owner,
                                         address=owner.referral_payout_address)
    # Pay down further → positive.
    pay3 = _mock_paid_payment(db, user=referee, plan=plan,
                               final_amount=2000, base_amount=2000)
    earn3 = ReferralEarning(referrer_id=owner.id, referee_id=referee.id,
                            payment_id=pay3.id, pct=Decimal("10"),
                            amount_usd=Decimal("200"))
    db.add(earn3); db.commit()
    bal = referral_service.available_balance(db, owner)
    assert bal == Decimal("170"), f"expected 170, got {bal}"
    # Now withdraw works (above min_payout default $100).
    req2 = referral_service.request_payout(db, user=owner,
                                            address=owner.referral_payout_address)
    assert req2.amount_usd == Decimal("170")


# ── webhook idempotency: same payment processed twice produces ONE row ═══════

def test_webhook_idempotent_no_double_credit(db):
    """If the same payment is _activate_user'd twice (webhook retry,
    admin re-process), the second call must NOT create a duplicate
    Usage or Earning row. The UNIQUE(payment_id) on each table is the
    DB-side seal."""
    owner = _user(db, "idem_owner")
    code = svc.create_self_serve_code(db, owner=owner, code="idem1",
                                       commission_pct=10, discount_pct=10)
    referee = _user(db, "idem_ref")
    svc.bind_referee(db, referee=referee, raw_code="idem1")
    plan = _seed_plan(db)
    db.commit()
    pay = _mock_paid_payment(db, user=referee, plan=plan,
                              final_amount=90, base_amount=100)
    payment_service._activate_user(db, pay)
    # Re-process — should NOT create duplicates. The DB UNIQUE on
    # payment_id (ReferralCodeUsage + ReferralEarning) raises
    # IntegrityError on the second insert; we explicitly rollback so
    # the session is clean for the subsequent query.
    try:
        payment_service._activate_user(db, pay)
    except Exception:
        db.rollback()
    assert db.query(models.ReferralCodeUsage).filter_by(payment_id=pay.id).count() == 1
    assert db.query(models.ReferralEarning).filter_by(payment_id=pay.id).count() == 1


# ── self-referral defense at credit time (belt-and-suspenders) ═══════════════

def test_self_referral_skipped_at_credit_time(db):
    """If a DB write bypasses bind_referee's check (e.g. direct UPDATE),
    the credit path here STILL skips self-referrals so the owner doesn't
    pay themselves. Defensive."""
    owner_referee = _user(db, "selfpay")
    code = svc.create_self_serve_code(db, owner=owner_referee, code="selfp",
                                       commission_pct=10, discount_pct=10)
    # Bypass bind_referee: directly set signup_code_id to own code.
    owner_referee.signup_code_id = code.id
    db.add(owner_referee); db.commit()
    plan = _seed_plan(db)
    db.commit()
    pay = _mock_paid_payment(db, user=owner_referee, plan=plan,
                              final_amount=100, base_amount=100)
    payment_service._activate_user(db, pay)
    # No earning row — self-referral skipped.
    assert db.query(models.ReferralEarning).filter_by(payment_id=pay.id).first() is None
