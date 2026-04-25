from fastapi import Depends, HTTPException, Header
from sqlalchemy.orm import Session

from backend.db.base import get_db  # re-export for API layer
from backend.db.models import User

__all__ = ["get_db", "get_current_user", "get_admin_user"]


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
    return user


def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user
