"""Startup check: every `/api/admin/*` route declares `get_admin_user`.

Forgetting `Depends(get_admin_user)` on a new admin endpoint is a
silent IDOR — the route looks fine, returns 200, leaks data. This
module walks the FastAPI router tree and asserts every admin-prefixed
route has that dependency in its chain (direct or via a sub-router).

Wired into app.py at startup so the process refuses to boot with an
unguarded admin route, and into tests/test_admin_guards.py so CI
catches it before deploy.
"""
from __future__ import annotations

from typing import Iterable

from fastapi import FastAPI
from fastapi.routing import APIRoute

from backend.api.deps import get_admin_user

# Admin paths must include the API prefix (`/api`) once mounted. We tolerate
# either form so the check works whether called pre- or post-mount.
_ADMIN_PATH_PREFIXES = ("/api/admin/", "/admin/")


def _route_has_admin_dep(route: APIRoute) -> bool:
    dep = route.dependant
    seen = set()
    stack = [dep]
    while stack:
        d = stack.pop()
        if id(d) in seen:
            continue
        seen.add(id(d))
        if d.call is get_admin_user:
            return True
        for sub in (d.dependencies or []):
            stack.append(sub)
    return False


def find_unguarded_admin_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Return [(method, path)] for every admin-prefixed route missing the
    `get_admin_user` dependency in its Depends chain."""
    out: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not any(route.path.startswith(p) for p in _ADMIN_PATH_PREFIXES):
            continue
        if _route_has_admin_dep(route):
            continue
        for method in (route.methods or {"GET"}):
            out.append((method, route.path))
    return out


def assert_admin_routes_guarded(app: FastAPI) -> None:
    """Fail-fast at app startup if any admin route is missing the guard."""
    leaks = find_unguarded_admin_routes(app)
    if not leaks:
        return
    formatted = "\n".join(f"  - {m} {p}" for m, p in leaks)
    raise RuntimeError(
        "Startup check FAILED: admin route(s) without get_admin_user guard:\n"
        + formatted
        + "\nAdd `_: User = Depends(get_admin_user)` to the endpoint signature."
    )


__all__ = [
    "assert_admin_routes_guarded",
    "find_unguarded_admin_routes",
]
