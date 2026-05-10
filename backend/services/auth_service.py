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


def create_token(user_id: int, *, ttl_minutes: int | None = None,
                  scope: str | None = None, extra: dict | None = None) -> str:
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
        # Scope marks short-lived tokens (e.g. "totp_challenge", "google_oauth_state")
        # so downstream `get_current_user` can refuse them on regular endpoints —
        # only the matching second-factor / callback route accepts the challenge.
        payload["scope"] = scope
    if extra:
        # Extra claims (e.g. `next` redirect in OAuth state). Reserved keys
        # like sub/exp/jti/scope are ignored to avoid accidental override.
        for k, v in extra.items():
            if k not in payload:
                payload[k] = v
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
    """Register a new user. Always created with is_admin=False.

    Admin grant has exactly ONE path: manual SQL on the host
    (`UPDATE users SET is_admin=TRUE WHERE …`). No client-controlled
    flag, no env-var auto-grant, no first-registration-wins race. Once
    a user is admin they stay admin until SQL flips it back — there is
    no API surface to elevate or demote.
    """
    uname = username.lower().strip()
    user = User(
        username=uname,
        email=email.lower().strip(),
        hashed_password=hash_password(password),
        is_admin=False,
        plan="basic",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, login: str, password: str) -> User | None:
    """Authenticate by email or username. Throttling lives in
    backend.services.login_throttle — this function only verifies the
    password and bumps an audit counter on the User row.

    The previous version auto-flipped is_blocked=True after 5 failures,
    which doubled as a DoS: anyone who knew a victim's username could
    lock them out. Replaced with progressive cooldowns at the API layer."""
    user = get_user_by_email(db, login) or get_user_by_username(db, login)
    if user is None:
        return None
    if not verify_password(password, user.hashed_password):
        try:
            user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
            db.commit()
        except Exception:
            db.rollback()
        return None
    if user.failed_login_attempts:
        try:
            user.failed_login_attempts = 0
            db.commit()
        except Exception:
            db.rollback()
    return user
