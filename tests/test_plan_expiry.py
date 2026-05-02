"""Plan expiry background job — unit-level coverage of the _run_once pass."""
from __future__ import annotations

from datetime import datetime, timedelta

from backend.db.models import User
from backend.services.plan_expiry_service import _run_once


def _seed_user(username, plan, expires_at, is_admin=False, email=None):
    """Session-level insert; shared with conftest's in-memory DB.

    Also sets `plan_id` to the matching Plan row so prod-mirror queries
    (`u.plan_id == free_plan.id`) resolve correctly. The legacy `plan`
    string is the user-visible label; `plan_id` is the FK that
    plan_service / plan_expiry actually read."""
    from backend.db.models import Plan
    from tests.conftest import _Session
    session = _Session()
    try:
        plan_row = session.query(Plan).filter(Plan.slug == plan).first()
        u = User(
            username=username,
            email=email or f"{username}@test.com",
            hashed_password="x",
            plan=plan,
            plan_id=plan_row.id if plan_row else None,
            plan_expires_at=expires_at,
            is_admin=is_admin,
        )
        session.add(u)
        session.commit()
        session.refresh(u)
        return u.id
    finally:
        session.close()


def _get_plan(user_id):
    from tests.conftest import _Session
    session = _Session()
    try:
        return session.query(User).filter(User.id == user_id).first().plan
    finally:
        session.close()


def test_expired_pro_gets_downgraded(client):
    """Expired full-plan user gets downgraded to free."""
    uid = _seed_user("alice", "full", datetime.utcnow() - timedelta(days=1))
    n = _run_once()
    assert n == 1
    assert _get_plan(uid) == "free"


def test_unexpired_plan_untouched(client):
    uid = _seed_user("bob", "full", datetime.utcnow() + timedelta(days=30))
    n = _run_once()
    assert n == 0
    assert _get_plan(uid) == "full"


def test_admin_not_affected(client):
    uid = _seed_user("admin", "unlim", datetime.utcnow() - timedelta(days=365),
                     is_admin=True)
    n = _run_once()
    assert n == 0
    assert _get_plan(uid) == "unlim"


def test_no_expiry_untouched(client):
    """plan_expires_at=None means permanent (admin-granted)."""
    uid = _seed_user("carol", "unlim", None)
    n = _run_once()
    assert n == 0
    assert _get_plan(uid) == "unlim"


def test_basic_no_op(client):
    """Free-tier users have no plan to expire from — even with stale
    plan_expires_at, no downgrade fires."""
    uid = _seed_user("dan", "free", datetime.utcnow() - timedelta(days=1))
    n = _run_once()
    assert n == 0
    assert _get_plan(uid) == "free"
