import logging
import time
from collections import defaultdict
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from backend.api.deps import get_db, get_current_user
from backend.db.models import User
from backend.schemas.auth import UserRegister, UserLogin, Token, UserOut
import backend.services.auth_service as svc

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("avalant.auth")

# ── Simple in-memory rate limiter ─────────────────────────────────────────────
# Tracks failed login attempts per IP: {ip: [timestamp, ...]}
_login_attempts: dict[str, list[float]] = defaultdict(list)
_lock = Lock()

_MAX_ATTEMPTS = 10   # per window
_WINDOW_SEC   = 60   # rolling window in seconds
_BLOCK_SEC    = 300  # block duration after exceeding limit (5 min)


def _check_rate_limit(ip: str):
    now = time.monotonic()
    with _lock:
        timestamps = _login_attempts[ip]
        # Drop entries outside the rolling window
        _login_attempts[ip] = [t for t in timestamps if now - t < _WINDOW_SEC]
        if len(_login_attempts[ip]) >= _MAX_ATTEMPTS:
            logger.warning("Rate limit exceeded for IP %s (%d attempts)", ip, len(_login_attempts[ip]))
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Please wait a few minutes and try again.",
                headers={"Retry-After": str(_BLOCK_SEC)},
            )


def _record_attempt(ip: str):
    with _lock:
        _login_attempts[ip].append(time.monotonic())


def _clear_attempts(ip: str):
    with _lock:
        _login_attempts.pop(ip, None)


def _get_ip(request: Request) -> str:
    # Respect reverse-proxy forwarded header
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ── Endpoints ─────────────────────────────────────────────────────────────────

def _set_session_cookie(response: Response, token: str) -> None:
    from settings import settings
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_DAYS * 86400,
        path="/",
    )


@router.post("/register", response_model=Token, status_code=201)
def register(body: UserRegister, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = _get_ip(request)
    _check_rate_limit(ip)
    if svc.get_user_by_email(db, body.email):
        _record_attempt(ip)
        logger.warning("Register attempt with existing email from IP %s", ip)
        raise HTTPException(status_code=409, detail="Email already registered")
    if svc.get_user_by_username(db, body.username):
        _record_attempt(ip)
        logger.warning("Register attempt with existing username from IP %s", ip)
        raise HTTPException(status_code=409, detail="Username already taken")
    user = svc.register_user(db, body.username, body.email, body.password)
    _clear_attempts(ip)
    logger.info("New user registered: %s (id=%d, admin=%s)", user.username, user.id, user.is_admin)
    token = svc.create_token(user.id)
    _set_session_cookie(response, token)
    return Token(access_token=token)


@router.post("/login", response_model=Token)
def login(body: UserLogin, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = _get_ip(request)
    _check_rate_limit(ip)
    user = svc.authenticate_user(db, body.login, body.password)
    if not user:
        _record_attempt(ip)
        logger.warning("Failed login attempt for %r from IP %s", body.login, ip)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if getattr(user, 'is_blocked', False):
        logger.warning("Blocked user login attempt: %s from IP %s", user.username, ip)
        raise HTTPException(status_code=403, detail="Your account has been blocked. Please contact support.")
    _clear_attempts(ip)
    logger.info("User logged in: %s (id=%d) from IP %s", user.username, user.id, ip)
    token = svc.create_token(user.id)
    _set_session_cookie(response, token)
    return Token(access_token=token)


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("session", path="/")
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user
