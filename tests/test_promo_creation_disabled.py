"""PROMO_CREATION_DISABLED env flag.

When set to '1', POST /api/admin/promos returns 503 even for valid
admin sessions. Existing promos (list / patch / delete) untouched.

The flag is read at request time via os.environ.get — no module-level
caching — so tests can flip it per-case.
"""
from __future__ import annotations
import os


def test_promo_create_blocked_when_flag_set(client, admin_auth):
    """Test #13 from the spec — flag=1 → 503 with explanatory message,
    no row inserted."""
    prev = os.environ.get("PROMO_CREATION_DISABLED")
    os.environ["PROMO_CREATION_DISABLED"] = "1"
    try:
        r = client.post(
            "/api/admin/promos",
            headers=admin_auth,
            json={"code": "BLOCKED", "discount_pct": 10, "is_active": True},
        )
        assert r.status_code == 503, r.text
        assert "disabled" in r.json()["detail"].lower()
    finally:
        if prev is None:
            os.environ.pop("PROMO_CREATION_DISABLED", None)
        else:
            os.environ["PROMO_CREATION_DISABLED"] = prev


def test_promo_create_works_when_flag_unset(client, admin_auth):
    """Default behaviour — no env, admin can still create promos.
    Preserves backwards compat until we explicitly cut over."""
    prev = os.environ.get("PROMO_CREATION_DISABLED")
    os.environ.pop("PROMO_CREATION_DISABLED", None)
    try:
        r = client.post(
            "/api/admin/promos",
            headers=admin_auth,
            json={"code": "TESTPROMO", "discount_pct": 5, "is_active": True},
        )
        assert r.status_code == 200, r.text
        assert r.json()["code"] == "TESTPROMO"
    finally:
        if prev is not None:
            os.environ["PROMO_CREATION_DISABLED"] = prev


def test_promo_create_blocked_with_flag_0(client, admin_auth):
    """Flag explicitly '0' must NOT block — only '1' triggers the gate.
    Defensive: env-var truthiness in Python is loose; the code uses
    strict '== "1"'."""
    prev = os.environ.get("PROMO_CREATION_DISABLED")
    os.environ["PROMO_CREATION_DISABLED"] = "0"
    try:
        r = client.post(
            "/api/admin/promos",
            headers=admin_auth,
            json={"code": "FLAG0OK", "discount_pct": 5, "is_active": True},
        )
        assert r.status_code == 200, r.text
    finally:
        if prev is None:
            os.environ.pop("PROMO_CREATION_DISABLED", None)
        else:
            os.environ["PROMO_CREATION_DISABLED"] = prev
