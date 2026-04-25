"""Authentication: password hashing, JWT creation and verification."""
import logging
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from backend.db.models import User
from settings import settings

logger = logging.getLogger("avalant.auth_service")
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Passwords ─────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


# ── JWT ───────────────────────────────────────────────────────────────────────

_ALGORITHM = "HS256"


def create_token(user_id: int, *, ttl_minutes: int | None = None, scope: str | None = None) -> str:
    import uuid
    if ttl_minutes is None:
        expire = datetime.now(timezone.utc) + timedelta(days=settings.ACCESS_TOKEN_EXPIRE_DAYS)
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    # `jti` is a unique token id — feeds the revocation list so a
    # logout / admin block can invalidate this exact session without
    # touching any other still-valid token for the same user.
    payload: dict = {"sub": str(user_id), "exp": expire, "jti": uuid.uuid4().hex}
    if scope:
        # Scope marks short-lived tokens (e.g. "totp_challenge") so
        # downstream `get_current_user` can refuse them on regular
        # endpoints — only the matching second-factor route accepts
        # the challenge.
        payload["scope"] = scope
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_ALGORITHM)


def decode_token(token: str) -> int | None:
    """Backward-compat thin wrapper — returns just the user_id. Callers
    that need the jti / exp use `decode_payload`."""
    payload = decode_payload(token)
    if not payload:
        return None
    try:
        return int(payload.get("sub"))
    except (TypeError, ValueError):
        return None


def decode_payload(token: str) -> dict | None:
    """Full payload decode — used by middleware that wants the jti for
    revocation checks + exp for TTL computation on logout."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[_ALGORITHM])
        return payload
    except (JWTError, KeyError, ValueError):
        return None


# ── User helpers ──────────────────────────────────────────────────────────────

def get_user_by_email(db: Session, email: str) -> User | None:
    return db.query(User).filter(User.email == email.lower()).first()


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.query(User).filter(User.username == username.lower()).first()


def get_user_by_id(db: Session, user_id: int) -> User | None:
    return db.query(User).filter(User.id == user_id).first()


def register_user(db: Session, username: str, email: str, password: str) -> User:
    """Register a new user.

    Admin promotion happens ONLY through one of these paths:
      1. INITIAL_ADMIN_USERNAME env matches the username being registered
         (set on the server, never via the API).
      2. AVALANT_ALLOW_FIRST_USER_ADMIN=1 env is explicitly set AND the
         users table is empty — strictly a dev/local convenience that has
         to be opted in. Production must NEVER set this.
      3. Manual SQL on the host: UPDATE users SET is_admin=TRUE WHERE …

    Every user registered via the public API is otherwise basic — no
    "first registration wins" races, no client-controlled flag, no
    admin-grant via this entry point.
    """
    import os
    from sqlalchemy import func

    uname = username.lower().strip()
    seed_name = (os.environ.get("INITIAL_ADMIN_USERNAME") or "").lower().strip()
    allow_first = (os.environ.get("AVALANT_ALLOW_FIRST_USER_ADMIN") or "").strip() == "1"

    make_admin = False
    if seed_name and uname == seed_name:
        make_admin = True
    elif allow_first and db.query(func.count(User.id)).scalar() == 0:
        make_admin = True

    user = User(
        username=uname,
        email=email.lower().strip(),
        hashed_password=hash_password(password),
        is_admin=make_admin,
        plan="unlim" if make_admin else "basic",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    if make_admin:
        logger.warning(
            "ADMIN seeded via register_user: username=%s seed_name=%s allow_first=%s",
            uname, seed_name or "<unset>", allow_first,
        )
    return user


def authenticate_user(db: Session, login: str, password: str) -> User | None:
    """Authenticate by email or username."""
    user = get_user_by_email(db, login) or get_user_by_username(db, login)
    if not user or not verify_password(password, user.hashed_password):
        return None
    return user
