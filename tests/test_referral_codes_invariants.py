"""Raw-INSERT tests for ReferralCode CHECK invariants.

These tests bypass the service layer entirely and hit the DB with raw SQL
so the load-bearing property of the split model — that the schema itself
rejects malformed codes — is proven directly. If any of these tests pass
when they should fail, the DB-side defense is broken.

The 8 cases mirror the spec's "защитные CHECK constraints":

  #5  pool 40% + admin_id=NULL  → ck_referral_codes_high_pool_needs_admin
  #6  pool 40% + admin_id=set   → OK
  #7  pool 50% + admin_id=set   → ck_referral_codes_total_cap  (global)

Plus boundary + consistency tests so a future schema rewrite that
"helpfully" loosens a CHECK gets caught by CI before it ships.
"""
from __future__ import annotations
import pytest
from sqlalchemy.exc import IntegrityError

from backend.db import models
from backend.db.base import SessionLocal


@pytest.fixture
def db():
    """Per-test session bound to the conftest-managed in-memory SQLite. The
    session-scoped _create_tables fixture in conftest.py has already run
    Base.metadata.create_all, so CHECK constraints declared in models'
    __table_args__ are live."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _make_user(db, username: str, is_admin: bool = False) -> models.User:
    """Minimal User row — only the columns our CHECKs touch.
    bcrypt'd password isn't relevant; just needs a non-null value."""
    u = models.User(
        username=username,
        email=f"{username}@x.test",
        hashed_password="x",
        is_admin=is_admin,
    )
    db.add(u)
    db.flush()
    return u


def _insert_code(db, **fields):
    """Direct ORM insert, returns the row or raises IntegrityError from the
    CHECK constraint. Uses session.execute on raw insert so the round-trip
    actually hits the constraints (ORM defaults won't paper over things)."""
    defaults = dict(
        code="TEST" + str(fields.get("_id", "X")),
        code_type="self_serve",
        created_by_admin_id=None,
        commission_pct=10,
        discount_pct=10,
    )
    defaults.update({k: v for k, v in fields.items() if not k.startswith("_")})
    row = models.ReferralCode(
        owner_id=fields["owner_id"],
        code=defaults["code"],
        code_type=defaults["code_type"],
        created_by_admin_id=defaults["created_by_admin_id"],
        commission_pct=defaults["commission_pct"],
        discount_pct=defaults["discount_pct"],
    )
    db.add(row)
    db.flush()
    return row


def test_low_pool_self_serve_ok(db):
    """Pool ≤25 + self_serve + NULL admin → fine. Baseline sanity."""
    u = _make_user(db, "u1")
    code = _insert_code(db, owner_id=u.id, _id=1,
                        commission_pct=15, discount_pct=10,
                        code_type='self_serve', created_by_admin_id=None)
    assert code.id


def test_high_pool_without_admin_blocked(db):
    """Test #5 — pool=40%, no admin → ck_referral_codes_high_pool_needs_admin.
    This is the SECURITY-CRITICAL invariant: a user-side endpoint that
    accidentally allows self_serve=40% is caught by the DB."""
    u = _make_user(db, "u2")
    with pytest.raises(IntegrityError) as exc:
        _insert_code(db, owner_id=u.id, _id=2,
                     commission_pct=25, discount_pct=15,
                     code_type='self_serve', created_by_admin_id=None)
    # We don't assert the exact constraint name (SQLite/PG differ on
    # surfacing); checking that some IntegrityError fires is enough.
    assert exc.type is IntegrityError


def test_high_pool_with_admin_ok(db):
    """Test #6 — pool=40% + admin_id set → OK (admin-attributed)."""
    admin = _make_user(db, "admin1", is_admin=True)
    owner = _make_user(db, "u3")
    code = _insert_code(db, owner_id=owner.id, _id=3,
                        commission_pct=20, discount_pct=20,
                        code_type='admin',
                        created_by_admin_id=admin.id)
    assert code.id
    assert code.created_by_admin_id == admin.id


def test_pool_above_45_blocked_even_for_admin(db):
    """Test #7 — pool=50% + admin → ck_referral_codes_total_cap.
    Global ceiling is HARD, not bypassable even by admin. Admin can
    issue high-pool codes (>25) but never exceed 45."""
    admin = _make_user(db, "admin2", is_admin=True)
    owner = _make_user(db, "u4")
    with pytest.raises(IntegrityError):
        _insert_code(db, owner_id=owner.id, _id=4,
                     commission_pct=30, discount_pct=20,
                     code_type='admin',
                     created_by_admin_id=admin.id)


def test_boundary_45_exact_admin_ok(db):
    """Boundary: 45% exact + admin → just inside the cap, must pass."""
    admin = _make_user(db, "admin3", is_admin=True)
    owner = _make_user(db, "u5")
    code = _insert_code(db, owner_id=owner.id, _id=5,
                        commission_pct=25, discount_pct=20,
                        code_type='admin',
                        created_by_admin_id=admin.id)
    assert code.id


def test_boundary_46_admin_blocked(db):
    """Boundary: 45.01% must fail. The Numeric(5,2) precision matters
    here — fractions caught by the same CHECK."""
    admin = _make_user(db, "admin4", is_admin=True)
    owner = _make_user(db, "u6")
    with pytest.raises(IntegrityError):
        _insert_code(db, owner_id=owner.id, _id=6,
                     commission_pct=25.01, discount_pct=20,
                     code_type='admin',
                     created_by_admin_id=admin.id)


def test_boundary_25_exact_self_serve_ok(db):
    """Boundary: 25% exact + self_serve → upper bound of self-serve, OK."""
    u = _make_user(db, "u7")
    code = _insert_code(db, owner_id=u.id, _id=7,
                        commission_pct=10, discount_pct=15,
                        code_type='self_serve', created_by_admin_id=None)
    assert code.id


def test_boundary_25_01_self_serve_blocked(db):
    """Boundary: 25.01% self_serve → ck_referral_codes_high_pool_needs_admin."""
    u = _make_user(db, "u8")
    with pytest.raises(IntegrityError):
        _insert_code(db, owner_id=u.id, _id=8,
                     commission_pct=10.01, discount_pct=15,
                     code_type='self_serve', created_by_admin_id=None)


def test_type_self_serve_with_admin_id_blocked(db):
    """Consistency: code_type=self_serve MUST have created_by_admin_id=NULL.
    A forged 'self_serve' label with an admin_id set is rejected — closes
    a possible bypass where a client sends type='self_serve' but the
    service accidentally stamps an admin_id."""
    admin = _make_user(db, "admin5", is_admin=True)
    owner = _make_user(db, "u9")
    with pytest.raises(IntegrityError):
        _insert_code(db, owner_id=owner.id, _id=9,
                     commission_pct=10, discount_pct=10,
                     code_type='self_serve',
                     created_by_admin_id=admin.id)


def test_type_admin_without_admin_id_blocked(db):
    """Consistency: code_type=admin MUST have created_by_admin_id non-NULL.
    Closes the symmetric bypass — labelling a row 'admin' to dodge the
    high-pool check without actually attributing it to a person."""
    owner = _make_user(db, "u10")
    with pytest.raises(IntegrityError):
        _insert_code(db, owner_id=owner.id, _id=10,
                     commission_pct=10, discount_pct=10,
                     code_type='admin',
                     created_by_admin_id=None)


def test_unknown_code_type_blocked(db):
    """Enum: only 'self_serve' and 'admin' are valid."""
    owner = _make_user(db, "u11")
    with pytest.raises(IntegrityError):
        _insert_code(db, owner_id=owner.id, _id=11,
                     commission_pct=10, discount_pct=10,
                     code_type='partner',
                     created_by_admin_id=None)


def test_negative_pct_blocked(db):
    """Non-negativity. Caught by ck_referral_codes_nonneg."""
    owner = _make_user(db, "u12")
    with pytest.raises(IntegrityError):
        _insert_code(db, owner_id=owner.id, _id=12,
                     commission_pct=-5, discount_pct=10,
                     code_type='self_serve', created_by_admin_id=None)


def test_case_insensitive_uniqueness(db):
    """LOWER(code) UNIQUE — 'Crypto' and 'crypto' cannot coexist."""
    owner = _make_user(db, "u13")
    _insert_code(db, owner_id=owner.id, _id=13, code="Crypto",
                 commission_pct=10, discount_pct=10,
                 code_type='self_serve', created_by_admin_id=None)
    with pytest.raises(IntegrityError):
        _insert_code(db, owner_id=owner.id, _id=14, code="CRYPTO",
                     commission_pct=10, discount_pct=10,
                     code_type='self_serve', created_by_admin_id=None)


def test_registration_uniq_referee_id(db):
    """Anti-reattribution: UNIQUE(referee_id) on registrations means a
    user can be bound to AT MOST ONE code, ever."""
    owner = _make_user(db, "u15")
    code_a = _insert_code(db, owner_id=owner.id, _id=15, code="alpha",
                          commission_pct=10, discount_pct=10,
                          code_type='self_serve', created_by_admin_id=None)
    code_b = _insert_code(db, owner_id=owner.id, _id=16, code="beta",
                          commission_pct=10, discount_pct=10,
                          code_type='self_serve', created_by_admin_id=None)
    referee = _make_user(db, "u16")
    db.add(models.ReferralCodeRegistration(code_id=code_a.id, referee_id=referee.id))
    db.flush()
    db.add(models.ReferralCodeRegistration(code_id=code_b.id, referee_id=referee.id))
    with pytest.raises(IntegrityError):
        db.flush()


def test_usage_uniq_payment_id(db):
    """Idempotency: a single payment can produce at most ONE usage row.
    Webhook retry / double-call cannot double-credit at the DB layer."""
    owner = _make_user(db, "u17")
    code = _insert_code(db, owner_id=owner.id, _id=17, code="gamma",
                        commission_pct=10, discount_pct=10,
                        code_type='self_serve', created_by_admin_id=None)
    referee = _make_user(db, "u18")
    # Seed a minimal Plan so Payment.plan_id FK is satisfied. The Plan
    # columns mirror prod's bare-min — only the FK target matters here.
    plan = models.Plan(slug="test-plan", name="Test", price_usd_monthly=100,
                      portfolio_limit=1, exchange_keys_per_venue=1,
                      trade_delay_ms=0)
    db.add(plan)
    db.flush()
    pay = models.Payment(
        user_id=referee.id,
        plan_id=plan.id,
        base_amount_usd=100,
        final_amount_usd=90,
        status="paid",
        provider_invoice_id="inv-test",
    )
    db.add(pay)
    db.flush()
    db.add(models.ReferralCodeUsage(
        code_id=code.id, referee_id=referee.id, payment_id=pay.id,
        payment_amount_usd=90, commission_earned=10, discount_applied=10,
    ))
    db.flush()
    db.add(models.ReferralCodeUsage(
        code_id=code.id, referee_id=referee.id, payment_id=pay.id,
        payment_amount_usd=90, commission_earned=10, discount_applied=10,
    ))
    with pytest.raises(IntegrityError):
        db.flush()
