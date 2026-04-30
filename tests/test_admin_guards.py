"""Defense-in-depth check: every /api/admin/* route declares the
`Depends(get_admin_user)` guard.

Forgetting this dependency on a new admin endpoint is a silent IDOR —
the route looks fine, returns 200, leaks data. The same check runs at
app startup (assert_admin_routes_guarded in app.py) so the process
refuses to boot if it ever regresses; this test catches it earlier in
CI before a buggy commit reaches deploy.
"""
from backend.services.admin_guard_check import find_unguarded_admin_routes


def test_every_admin_route_requires_admin_guard(client):
    # Use the live FastAPI app from the test client. Importing app.app
    # directly works too, but going through `client` matches the rest of
    # the suite and avoids the lifespan / Alembic side-effects.
    app = client.app
    leaks = find_unguarded_admin_routes(app)
    assert leaks == [], (
        "Admin route(s) missing Depends(get_admin_user):\n"
        + "\n".join(f"  - {m} {p}" for m, p in leaks)
        + "\nFix: add `_: User = Depends(get_admin_user)` to the signature."
    )
