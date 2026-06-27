from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Header, Request
from sqlalchemy.orm import Session

from backend.db.base import get_db  # re-export for API layer
from backend.db.models import User

__all__ = ["get_db", "get_current_user", "get_admin_user"]

# Throttle window for the last_active_at bump — one write per user per minute.
# Most active users hit 5-20 authenticated routes per minute (screener WS auth
# included), so without this we'd be doing one UPDATE per request. With it
# the DB writes scale by active-user-count, not by request-rate.
_LAST_ACTIVE_THROTTLE = timedelta(minutes=1)


def _bump_last_active(db: Session, user: User) -> None:
    """Update users.last_active_at iff the cached value is stale (>1 min).
    Swallows DB errors — presence tracking is best-effort, must never fail
    the request."""
    try:
        now = datetime.utcnow()
        prev = user.last_active_at
        if prev is None or (now - prev) >= _LAST_ACTIVE_THROTTLE:
            user.last_active_at = now
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization[7:]
    from backend.services.auth_service import decode_token, decode_payload, get_user_by_id
    from backend.services.auth_cache import get_cached_user, cache_user
    from backend.services import token_blacklist

    # Fast path: Redis hit returns a lightweight user stub. Skips JWT decode
    # and DB lookup — the single biggest repeat cost on hot endpoints like
    # /api/screener/*. Cache invalidated by /api/admin block/plan mutations.
    # We still decode the JWT once to grab `jti` for the blacklist check —
    # without that the cache hit would let a revoked token through.
    payload = decode_payload(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    # Scoped tokens (e.g. `totp_challenge`) are only valid on the matching
    # second-factor route, never as a session credential. Bounce them here
    # so a leaked challenge can't masquerade as a full session.
    if payload.get("scope"):
        raise HTTPException(status_code=401, detail="Token scope insufficient for this resource")
    jti = payload.get("jti")
    if jti and token_blacklist.is_revoked(jti):
        raise HTTPException(status_code=401, detail="Session revoked")

    cached = get_cached_user(token)
    if cached is not None:
        uid, is_blocked, is_admin = cached
        if is_blocked:
            raise HTTPException(status_code=403, detail="Account is blocked")
        user = db.query(User).get(uid)
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        # Online presence — fetcher's user-stream supervisor reads this
        # to decide whether to keep WS connections open for this user.
        try:
            from backend.services.online_presence import heartbeat as _online_hb
            _online_hb(uid)
        except Exception:
            pass
        _bump_last_active(db, user)
        return user

    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token payload")
    user = get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if getattr(user, 'is_blocked', False):
        raise HTTPException(status_code=403, detail="Account is blocked")
    cache_user(token, user.id, bool(user.is_blocked), bool(user.is_admin))
    try:
        from backend.services.online_presence import heartbeat as _online_hb
        _online_hb(user.id)
    except Exception:
        pass
    _bump_last_active(db, user)
    return user


def get_admin_user(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """Admin gate. A logged-in non-admin who hits this is treated as a
    probe: their account is auto-blocked, audit-logged, and admins get a
    TG ping. Anonymous callers fail at get_current_user before this and
    are NOT auto-banned (browser address-bar typing / link-clicking
    shouldn't trip the honeypot)."""
    if not current_user.is_admin:
        try:
            from backend.services import honeypot_service
            ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() \
                 or (request.client.host if request.client else None)
            honeypot_service.trip(
                db, current_user,
                request_ip=ip,
                request_path=request.url.path,
                request_method=request.method,
                reason="admin_endpoint_probe",
            )
        except Exception:
            # Never let honeypot bookkeeping bypass the 403 — that's the
            # actual security control.
            pass
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user
