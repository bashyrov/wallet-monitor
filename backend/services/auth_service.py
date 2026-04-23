"""Authentication: password hashing, JWT creation and verification."""
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from backend.db.models import User
from settings import settings

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Passwords ─────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


# ── JWT ───────────────────────────────────────────────────────────────────────

_ALGORITHM = "HS256"


def create_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=settings.ACCESS_TOKEN_EXPIRE_DAYS)
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_ALGORITHM)


def decode_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[_ALGORITHM])
        return int(payload["sub"])
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

    Admin seeding:
      - In an empty DB (dev / local), the first user is seeded as admin +
        unlim so you have working access immediately.
      - In production, set INITIAL_ADMIN_USERNAME so only that specific
        username becomes admin — removes the "whoever registers first wins"
        race that the legacy logic had.
    """
    import os
    from sqlalchemy import func

    uname = username.lower().strip()
    is_first = db.query(func.count(User.id)).scalar() == 0
    seed_name = (os.environ.get("INITIAL_ADMIN_USERNAME") or "").lower().strip()

    if seed_name:
        # Explicit admin seed: only this username gets admin, even on an
        # empty DB. Random race-winner registrations stay on `basic`.
        make_admin = (uname == seed_name)
    else:
        # Legacy first-wins behavior — kept for dev/local where seed isn't set.
        make_admin = is_first

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
    return user


def authenticate_user(db: Session, login: str, password: str) -> User | None:
    """Authenticate by email or username."""
    user = get_user_by_email(db, login) or get_user_by_username(db, login)
    if not user or not verify_password(password, user.hashed_password):
        return None
    return user
