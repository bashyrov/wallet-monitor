import logging
import time
from collections import defaultdict
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
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
def me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    out = UserOut.model_validate(current_user)
    out.tg_linked = bool(current_user.tg_chat_id)
    # Enrich with effective limits so the frontend picker / pricing page
    # can show the right cap without round-tripping to /api/plans.
    try:
        from backend.services import plan_service as _ps
        lim = _ps.effective_limits(db, current_user)
        out.plan_id = lim.plan_id
        out.portfolio_limit = lim.portfolio_limit
        out.exchange_keys_per_venue = lim.exchange_keys_per_venue
        out.is_plan_expired = lim.is_expired
        out.wallet_limit = lim.portfolio_limit
    except Exception:
        pass
    return out


from pydantic import BaseModel as _BM


class _UserPatch(_BM):
    tg_username: str | None = None


# ── Telegram login widget ────────────────────────────────────────────────────
class _TgWidgetAuth(_BM):
    id: int
    auth_date: int
    hash: str
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None


@router.post("/tg-login", response_model=Token)
def tg_login(body: _TgWidgetAuth, request: Request, response: Response, db: Session = Depends(get_db)):
    """Accept Telegram Login Widget payload, verify HMAC signature, issue JWT."""
    from backend.services.tg_auth_service import login_via_widget
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    _check_rate_limit(ip)
    try:
        user, token, created = login_via_widget(db, body.model_dump(exclude_none=True))
    except ValueError as e:
        _record_attempt(ip)
        logger.info("TG login rejected from %s: %s", ip, e)
        raise HTTPException(401, "Telegram authentication failed")
    response.set_cookie("session", token, httponly=True, secure=False, samesite="lax", max_age=60*60*24*30)
    logger.info("TG login: user_id=%s created=%s tg_id=%s", user.id, created, user.tg_id)
    return Token(access_token=token, token_type="bearer")


# ── One-time link token for profile ──────────────────────────────────────────
# ── Login-by-Bot (no auth required) ──────────────────────────────────────────
@router.post("/tg-bot-login")
def tg_bot_login_start(db: Session = Depends(get_db)):
    """Generate a one-time token for login via bot. Returns deep_link to open."""
    from backend.services.tg_auth_service import issue_login_token
    return issue_login_token(db)


# ── Password reset flow ──────────────────────────────────────────────────────
import hashlib as _hashlib
import os as _os
import secrets as _secrets
from datetime import datetime as _dt, timedelta as _td

from backend.db.models import PasswordResetToken, EmailVerifyToken


class _PwResetRequest(_BM):
    email: str


class _PwResetConfirm(_BM):
    token: str
    new_password: str


_PW_RESET_TTL_MIN = 15


def _hash_token(raw: str) -> str:
    return _hashlib.sha256(raw.encode()).hexdigest()


def _mailer_configured() -> bool:
    """True when an SMTP / transactional-email backend is wired. Until then
    the request endpoint returns the raw token in the response so dev/admin
    can hand it to the user manually. Do NOT return it in prod."""
    return bool(_os.environ.get("SMTP_HOST") or _os.environ.get("SENDGRID_API_KEY"))


@router.post("/password-reset/request")
def password_reset_request(body: _PwResetRequest, request: Request, db: Session = Depends(get_db)):
    """Issue a password-reset token for the user with the given email.

    Rate-limited upstream by nginx (5 req/min on /api/auth/*). Always
    returns 200 with a generic message — never leaks whether the email
    is registered. If `SMTP_HOST` / `SENDGRID_API_KEY` is unset, the
    response also includes `dev_token` so ops can complete the flow
    manually; that field is never set in prod once mail is configured.
    """
    email = (body.email or "").strip().lower()
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    _check_rate_limit(ip)

    user = svc.get_user_by_email(db, email)
    generic = {"status": "ok", "message": "If that email is registered, a reset link has been sent."}
    if not user:
        return generic

    # Invalidate any un-used tokens for this user — one at a time only.
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None),
    ).delete(synchronize_session=False)

    raw = _secrets.token_urlsafe(32)
    row = PasswordResetToken(
        user_id=user.id,
        token_hash=_hash_token(raw),
        expires_at=_dt.utcnow() + _td(minutes=_PW_RESET_TTL_MIN),
    )
    db.add(row)
    db.commit()

    # TODO: wire a real mailer (SMTP / SendGrid / Resend) under
    #       _mailer_configured(). Until then, expose the token to ops.
    if _mailer_configured():
        # Send email here. Still a stub — mailer integration is a followup.
        logger.info("password-reset: token issued for uid=%s (mail path)", user.id)
        return generic
    logger.info("password-reset: token issued for uid=%s (dev mode — exposed in response)", user.id)
    return {**generic, "dev_token": raw, "expires_in_min": _PW_RESET_TTL_MIN}


@router.post("/password-reset/confirm")
def password_reset_confirm(body: _PwResetConfirm, request: Request, db: Session = Depends(get_db)):
    """Exchange a valid reset token for a new password."""
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    _check_rate_limit(ip)

    if not body.token or not body.new_password or len(body.new_password) < 8:
        raise HTTPException(400, "Token and a password ≥ 8 chars are required")

    h = _hash_token(body.token)
    row = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == h).first()
    if not row or row.used_at is not None or row.expires_at < _dt.utcnow():
        _record_attempt(ip)
        raise HTTPException(400, "Token invalid or expired")

    user = db.query(User).filter(User.id == row.user_id).first()
    if not user:
        raise HTTPException(400, "Token invalid or expired")

    user.hashed_password = svc.hash_password(body.new_password)
    row.used_at = _dt.utcnow()
    db.commit()

    # Invalidate the Redis auth cache so the just-rotated password doesn't
    # coexist with a stale session cached from the old one.
    try:
        from backend.services.auth_cache import invalidate_user
        invalidate_user(user.id)
    except Exception:
        pass

    logger.info("password-reset: password changed for uid=%s", user.id)
    return {"status": "ok"}


# ── Email verification ───────────────────────────────────────────────────────
_EMAIL_VERIFY_TTL_HOURS = 24


class _EmailVerifyConfirm(_BM):
    token: str


@router.post("/email-verify/request")
def email_verify_request(request: Request, current_user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """Issue a verification token for the current user's email. No-op if
    already verified — returns {already_verified: True}. Dev mode includes
    dev_token in the response; prod (mailer configured) only sends the link."""
    ip = _get_ip(request)
    _check_rate_limit(ip)

    if current_user.email_verified_at is not None:
        return {"status": "ok", "already_verified": True}

    db.query(EmailVerifyToken).filter(
        EmailVerifyToken.user_id == current_user.id,
        EmailVerifyToken.used_at.is_(None),
    ).delete(synchronize_session=False)

    raw = _secrets.token_urlsafe(32)
    row = EmailVerifyToken(
        user_id=current_user.id,
        token_hash=_hash_token(raw),
        expires_at=_dt.utcnow() + _td(hours=_EMAIL_VERIFY_TTL_HOURS),
    )
    db.add(row)
    db.commit()

    out = {"status": "ok", "email": current_user.email}
    if _mailer_configured():
        logger.info("email-verify: token issued for uid=%s (mail path)", current_user.id)
    else:
        logger.info("email-verify: token issued for uid=%s (dev mode)", current_user.id)
        out["dev_token"] = raw
        out["expires_in_hours"] = _EMAIL_VERIFY_TTL_HOURS
    return out


@router.post("/email-verify/confirm")
def email_verify_confirm(body: _EmailVerifyConfirm, request: Request, db: Session = Depends(get_db)):
    """Exchange a valid verify-token for email_verified_at=now()."""
    ip = _get_ip(request)
    _check_rate_limit(ip)

    if not body.token:
        raise HTTPException(400, "Token is required")

    h = _hash_token(body.token)
    row = db.query(EmailVerifyToken).filter(EmailVerifyToken.token_hash == h).first()
    if not row or row.used_at is not None or row.expires_at < _dt.utcnow():
        _record_attempt(ip)
        raise HTTPException(400, "Token invalid or expired")

    user = db.query(User).filter(User.id == row.user_id).first()
    if not user:
        raise HTTPException(400, "Token invalid or expired")

    if user.email_verified_at is None:
        user.email_verified_at = _dt.utcnow()
    row.used_at = _dt.utcnow()
    db.commit()

    try:
        from backend.services.auth_cache import invalidate_user
        invalidate_user(user.id)
    except Exception:
        pass

    logger.info("email-verify: email confirmed for uid=%s", user.id)
    return {"status": "ok", "email_verified_at": user.email_verified_at.isoformat()}


@router.get("/tg-bot-login")
def tg_bot_login_check(token: str = Query(...), response: Response = None):
    """Poll: has the user pressed Start in the bot yet?"""
    from backend.services.tg_auth_service import check_login_token
    result = check_login_token(token)
    if result.get("status") == "ok" and result.get("access_token"):
        response.set_cookie("session", result["access_token"],
                            httponly=True, secure=False, samesite="lax", max_age=60*60*24*30)
    return result


@router.post("/me/tg-link-token")
def tg_link_token(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Generate a short-lived single-use link token. Returns the bot deep link
    the user should tap to bind their Telegram chat to this Avalant account."""
    from backend.services.tg_auth_service import issue_link_token
    return issue_link_token(db, current_user.id)


@router.delete("/me/tg-link", status_code=204)
def tg_unlink(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Clear Telegram link on the user row. Does not prevent logging back in
    via widget — tg_id can be re-attached."""
    current_user.tg_chat_id = None
    current_user.tg_id = None
    current_user.tg_username = None
    db.commit()
    return Response(status_code=204)


class _DeleteMeBody(_BM):
    password: str


@router.delete("/me")
def delete_me(body: _DeleteMeBody, response: Response,
              current_user: User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    """Self-service account deletion. Requires current password as a
    second factor so a stolen session token alone can't nuke the
    account. Admins cannot delete themselves via this endpoint — they
    have to demote first. Cascade drops wallets, tags, snapshots, and
    arb alerts via the FKs defined in models.py."""
    if current_user.is_admin:
        raise HTTPException(status_code=400, detail="Admins cannot self-delete; demote first")
    if not svc.verify_password(body.password, current_user.hashed_password):
        raise HTTPException(status_code=401, detail="Password incorrect")

    uid = current_user.id
    username = current_user.username
    db.delete(current_user)
    db.commit()

    # Clear Redis auth cache so the just-deleted user's token stops
    # resolving to a phantom row.
    try:
        from backend.services.auth_cache import invalidate_user
        invalidate_user(uid)
    except Exception:
        pass

    # Drop the session cookie
    response.delete_cookie("session")
    logger.info("user self-deleted: uid=%s username=%s", uid, username)
    return {"status": "ok", "deleted_user_id": uid}


import re as _re
_TG_USERNAME_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")  # 5-32, must start with letter


@router.patch("/me", response_model=UserOut)
def patch_me(body: _UserPatch, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.tg_username is not None:
        tg = body.tg_username.strip().lstrip("@") or None
        if tg is not None and not _TG_USERNAME_RE.match(tg):
            from fastapi import HTTPException
            raise HTTPException(400, "tg_username must be 5-32 chars, start with a letter, letters/digits/underscore only")
        # If username changed, invalidate the previous chat link
        if tg != current_user.tg_username:
            current_user.tg_chat_id = None
        current_user.tg_username = tg
        db.commit()
        db.refresh(current_user)
    out = UserOut.model_validate(current_user)
    out.tg_linked = bool(current_user.tg_chat_id)
    return out
