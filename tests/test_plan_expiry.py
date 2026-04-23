"""Plan expiry background job — unit-level coverage of the _run_once pass."""
from __future__ import annotations

from datetime import datetime, timedelta

from backend.db.models import User
from backend.services.plan_expiry_service import _run_once


def _seed_user(username, plan, expires_at, is_admin=False, email=None):
    """Session-level insert; shared with conftest's in-memory DB."""
    from tests.conftest import _Session
    session = _Session()
    try:
        u = User(
            username=username,
            email=email or f"{username}@test.com",
            hashed_password="x",
            plan=plan,
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
    uid = _seed_user("alice", "pro", datetime.utcnow() - timedelta(days=1))
    n = _run_once()
    assert n == 1
    assert _get_plan(uid) == "basic"


def test_unexpired_plan_untouched(client):
    uid = _seed_user("bob", "pro", datetime.utcnow() + timedelta(days=30))
    n = _run_once()
    assert n == 0
    assert _get_plan(uid) == "pro"


def test_admin_not_affected(client):
    uid = _seed_user("admin", "unlim", datetime.utcnow() - timedelta(days=365),
                     is_admin=True)
    n = _run_once()
    assert n == 0
    assert _get_plan(uid) == "unlim"


def test_no_expiry_untouched(client):
    """plan_expires_at=None means permanent (enterprise / hand-set).
    Must not be downgraded."""
    uid = _seed_user("carol", "enterprise", None)
    n = _run_once()
    assert n == 0
    assert _get_plan(uid) == "enterprise"


def test_basic_no_op(client):
    """Basic users have no plan to expire from."""
    uid = _seed_user("dan", "basic", datetime.utcnow() - timedelta(days=1))
    n = _run_once()
    assert n == 0
    assert _get_plan(uid) == "basic"
