"""Service-layer tests for referral_code_service.

These bypass HTTP and call the pure-Python helpers directly. They prove
that the service layer FAILS GRACEFULLY before the DB CHECK constraint
would fire — users see a clear error, not "IntegrityError".

The raw-INSERT tests in test_referral_codes_invariants.py prove the DB
backstop. This file proves the front-line gate.
"""
from __future__ import annotations
import pytest
from decimal import Decimal

from backend.db import models
from backend.db.base import SessionLocal
from backend.services import referral_code_service as svc


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _user(db, name: str, is_admin: bool = False) -> models.User:
    u = models.User(username=name, email=f"{name}@x.test",
                    hashed_password="x", is_admin=is_admin)
    db.add(u); db.flush()
    return u


# ── format ═══════════════════════════════════════════════════════════════════

def test_code_format_too_short(db):
    u = _user(db, "fmt1")
    with pytest.raises(svc.CodeFormatError):
        svc.create_self_serve_code(db, owner=u, code="abc",
                                    commission_pct=5, discount_pct=5)


def test_code_format_bad_charset(db):
    u = _user(db, "fmt2")
    with pytest.raises(svc.CodeFormatError):
        svc.create_self_serve_code(db, owner=u, code="bad code!",
                                    commission_pct=5, discount_pct=5)


def test_code_format_too_long(db):
    u = _user(db, "fmt3")
    with pytest.raises(svc.CodeFormatError):
        svc.create_self_serve_code(db, owner=u, code="x" * 33,
                                    commission_pct=5, discount_pct=5)


def test_code_format_valid_alphanumeric_dash_underscore(db):
    u = _user(db, "fmt4")
    code = svc.create_self_serve_code(db, owner=u, code="My-Code_2024",
                                       commission_pct=5, discount_pct=5)
    assert code.code == "My-Code_2024"


# ── self-serve cap (test #2 in spec) ═════════════════════════════════════════

def test_self_serve_cap_25_exact_ok(db):
    u = _user(db, "ss1")
    code = svc.create_self_serve_code(db, owner=u, code="ss25ok",
                                       commission_pct=10, discount_pct=15)
    assert code.id


def test_self_serve_cap_above_25_rejected(db):
    u = _user(db, "ss2")
    with pytest.raises(svc.PoolCapExceededError):
        svc.create_self_serve_code(db, owner=u, code="ss26bad",
                                    commission_pct=15, discount_pct=15)


def test_self_serve_25_01_rejected_precision(db):
    u = _user(db, "ss3")
    with pytest.raises(svc.PoolCapExceededError):
        svc.create_self_serve_code(db, owner=u, code="ss2501",
                                    commission_pct=Decimal("10.01"),
                                    discount_pct=15)


# ── admin cap (test #3 in spec) ══════════════════════════════════════════════

def test_admin_cap_45_exact_ok(db):
    admin = _user(db, "a1", is_admin=True)
    owner = _user(db, "ao1")
    code = svc.create_admin_code(db, admin=admin, owner_id=owner.id,
                                  code="a45ok", commission_pct=25, discount_pct=20)
    assert code.id
    assert code.code_type == "admin"
    assert code.created_by_admin_id == admin.id


def test_admin_cap_46_rejected(db):
    admin = _user(db, "a2", is_admin=True)
    with pytest.raises(svc.PoolCapExceededError):
        svc.create_admin_code(db, admin=admin, owner_id=None,
                              code="a46bad", commission_pct=25, discount_pct=21)


def test_admin_default_owner_is_admin(db):
    admin = _user(db, "a3", is_admin=True)
    code = svc.create_admin_code(db, admin=admin, owner_id=None,
                                  code="aself", commission_pct=5, discount_pct=5)
    assert code.owner_id == admin.id


def test_admin_owner_id_must_exist(db):
    admin = _user(db, "a4", is_admin=True)
    with pytest.raises(svc.CodeServiceError) as exc:
        svc.create_admin_code(db, admin=admin, owner_id=99999,
                              code="aghost", commission_pct=5, discount_pct=5)
    # NOT IntegrityError — must be a clean service-level message
    assert "does not exist" in str(exc.value).lower()


# ── case-insensitive uniqueness ══════════════════════════════════════════════

def test_collision_case_insensitive(db):
    u = _user(db, "ci1")
    svc.create_self_serve_code(db, owner=u, code="Promo",
                                commission_pct=5, discount_pct=5)
    with pytest.raises(svc.CodeTakenError):
        svc.create_self_serve_code(db, owner=u, code="PROMO",
                                    commission_pct=5, discount_pct=5)


def test_lookup_case_insensitive(db):
    u = _user(db, "ci2")
    svc.create_self_serve_code(db, owner=u, code="UPPERED",
                                commission_pct=5, discount_pct=5)
    assert svc.find_code_by_string(db, "uppered") is not None
    assert svc.find_code_by_string(db, "Uppered") is not None
    assert svc.find_code_by_string(db, "  uppered  ") is not None


def test_lookup_missing_returns_none(db):
    assert svc.find_code_by_string(db, "nope") is None
    assert svc.find_code_by_string(db, "") is None
    assert svc.find_code_by_string(db, None) is None


# ── owner cap 50 (#12 — many codes per user, with anti-squat) ════════════════

def test_owner_can_have_multiple_codes(db):
    u = _user(db, "many1")
    for i in range(3):
        svc.create_self_serve_code(db, owner=u, code=f"many{i}",
                                    commission_pct=5, discount_pct=5)
    assert svc.count_owner_codes(db, u.id) == 3


def test_owner_cap_50(db):
    """50 codes per owner is the anti-squat cap. 51st rejected."""
    u = _user(db, "squat")
    for i in range(svc.CODES_PER_OWNER_MAX):
        svc.create_self_serve_code(db, owner=u, code=f"squat{i:03d}",
                                    commission_pct=1, discount_pct=1)
    with pytest.raises(svc.OwnerCodeCapError):
        svc.create_self_serve_code(db, owner=u, code="squat999",
                                    commission_pct=1, discount_pct=1)


# ── registration cap 15 (#8 — 16th referee blocked) ══════════════════════════

def test_registration_cap_15(db):
    owner = _user(db, "rcap")
    code = svc.create_self_serve_code(db, owner=owner, code="rcap1",
                                       commission_pct=5, discount_pct=5)
    for i in range(svc.REGISTRATIONS_CAP):
        ref = _user(db, f"rcap_ref_{i}")
        bound = svc.bind_referee(db, referee=ref, raw_code="rcap1")
        assert bound is not None
    overflow = _user(db, "rcap_overflow")
    with pytest.raises(svc.RegistrationCapError):
        svc.bind_referee(db, referee=overflow, raw_code="rcap1")


def test_bind_silent_when_code_missing(db):
    ref = _user(db, "silentref")
    assert svc.bind_referee(db, referee=ref, raw_code="nope") is None
    # User stays unbound — silent skip per spec
    assert ref.signup_code_id is None


def test_bind_silent_on_empty_code(db):
    ref = _user(db, "silentref2")
    assert svc.bind_referee(db, referee=ref, raw_code="") is None
    assert svc.bind_referee(db, referee=ref, raw_code=None) is None


# ── self-referral guard (spec — owner != referee) ═══════════════════════════

def test_self_referral_blocked_at_bind(db):
    u = _user(db, "selfref")
    svc.create_self_serve_code(db, owner=u, code="mineown",
                                commission_pct=5, discount_pct=5)
    with pytest.raises(svc.SelfReferralError):
        svc.bind_referee(db, referee=u, raw_code="mineown")
    assert u.signup_code_id is None  # not bound


# ── usage cap 5 — count_non_reversed_usages drives the cap ═══════════════════

def test_usages_count_excludes_reversed(db):
    owner = _user(db, "ucount_o")
    ref = _user(db, "ucount_r")
    code = svc.create_self_serve_code(db, owner=owner, code="ucnt",
                                       commission_pct=10, discount_pct=10)
    # Seed plan + payments to satisfy FK
    plan = models.Plan(slug="ucnt-plan", name="x", price_usd_monthly=100,
                       portfolio_limit=1, exchange_keys_per_venue=1, trade_delay_ms=0)
    db.add(plan); db.flush()
    for i in range(3):
        pay = models.Payment(user_id=ref.id, plan_id=plan.id,
                             base_amount_usd=100, final_amount_usd=90,
                             status="paid", provider_invoice_id=f"inv-{i}")
        db.add(pay); db.flush()
        u = models.ReferralCodeUsage(
            code_id=code.id, referee_id=ref.id, payment_id=pay.id,
            payment_amount_usd=90, commission_earned=10, discount_applied=10,
        )
        db.add(u); db.flush()
    assert svc.count_non_reversed_usages(db, code.id, ref.id) == 3
    # Reverse one — count drops
    from datetime import datetime
    db.query(models.ReferralCodeUsage).filter_by(payment_id=db.query(models.Payment).first().id).update(
        {"reversed_at": datetime.utcnow(), "reversal_reason": "test"}
    )
    db.flush()
    assert svc.count_non_reversed_usages(db, code.id, ref.id) == 2


# ── effective % helpers — clamp + cap-5 zero-out ════════════════════════════

def test_effective_discount_zero_when_cap_reached(db):
    owner = _user(db, "eff_o")
    code = svc.create_self_serve_code(db, owner=owner, code="eff1",
                                       commission_pct=5, discount_pct=20)
    assert svc.effective_discount_pct(code, usage_count=0) == Decimal("20")
    assert svc.effective_discount_pct(code, usage_count=4) == Decimal("20")
    assert svc.effective_discount_pct(code, usage_count=5) == Decimal("0")
    assert svc.effective_discount_pct(code, usage_count=99) == Decimal("0")


def test_effective_commission_zero_on_no_code():
    assert svc.effective_commission_pct(None, usage_count=0) == Decimal("0")
    assert svc.effective_discount_pct(None, usage_count=0) == Decimal("0")


# ── serialization — commission HIDDEN in preview ═════════════════════════════

def test_preview_hides_commission(db):
    owner = _user(db, "prev_o")
    code = svc.create_self_serve_code(db, owner=owner, code="prev1",
                                       commission_pct=15, discount_pct=5)
    pub = svc.serialize_code_for_preview(code, db=db)
    assert "commission_pct" not in pub
    assert "owner_id" not in pub
    assert pub["discount_pct"] == 5.0
    assert pub["is_open"] is True


def test_owner_view_shows_commission(db):
    owner = _user(db, "ov_o")
    code = svc.create_self_serve_code(db, owner=owner, code="ov1c",
                                       commission_pct=10, discount_pct=10)
    full = svc.serialize_code_for_owner(code, db=db)
    assert full["commission_pct"] == 10.0
    assert full["discount_pct"] == 10.0
    assert full["registrations_remaining"] == svc.REGISTRATIONS_CAP


# ── generate_unique_code — collision-avoidance ═══════════════════════════════

def test_generate_unique_code_returns_unique(db):
    seen = set()
    for _ in range(5):
        c = svc.generate_unique_code(db, length=7)
        assert len(c) == 7
        assert c not in seen
        seen.add(c)


# ── owner cap defends both self_serve AND admin paths ═══════════════════════

def test_owner_cap_applies_to_admin_creates_too(db):
    """The 50-per-owner cap must apply regardless of code_type — otherwise
    an admin could squat on namespace for a user past 50."""
    admin = _user(db, "adm_squat", is_admin=True)
    target = _user(db, "target_squat")
    for i in range(svc.CODES_PER_OWNER_MAX):
        svc.create_self_serve_code(db, owner=target, code=f"t{i:03d}",
                                    commission_pct=1, discount_pct=1)
    with pytest.raises(svc.OwnerCodeCapError):
        svc.create_admin_code(db, admin=admin, owner_id=target.id,
                              code="adminxxl", commission_pct=20, discount_pct=20)
