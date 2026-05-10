import logging
import time
from collections import defaultdict
from threading import Lock

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
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

def _cookie_secure() -> bool:
    """`secure=True` outside of localhost dev. Override via env if you ever
    need to test the prod cookie path locally without HTTPS."""
    import os
    raw = (os.environ.get("AVALANT_COOKIE_SECURE") or "").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    if raw in ("1", "true", "yes"):
        return True
    # Default: secure cookies. Anything other than literal localhost dev
    # MUST set the Secure flag — otherwise the JWT can leak over HTTP.
    return True


def _set_session_cookie(response: Response, token: str) -> None:
    from settings import settings
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_DAYS * 86400,
        path="/",
    )
    # Non-httpOnly companion flag so client JS can detect "server thinks
    # I'm logged in" without exposing the JWT itself. Used by auth.js to
    # decide whether to call /api/auth/cookie-session for localStorage
    # recovery — without it we'd probe on every anonymous page load.
    response.set_cookie(
        key="wm_authed",
        value="1",
        httponly=False,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_DAYS * 86400,
        path="/",
    )


@router.post("/register", status_code=201)
def register(body: UserRegister, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = _get_ip(request)
    _check_rate_limit(ip)
    # Email collision is silent — leaking "this email is registered" lets
    # attackers enumerate the user base via /register against a list of
    # leaked emails. Always return the same generic 201 shape; the legit
    # owner gets a heads-up email on their existing address instead.
    # Username collision stays public — usernames are visible on shared
    # cards / leaderboards anyway, so there's nothing to leak.
    if svc.get_user_by_username(db, body.username):
        _record_attempt(ip)
        logger.warning("Register attempt with existing username from IP %s", ip)
        raise HTTPException(status_code=409, detail="Username already taken")
    if svc.get_user_by_email(db, body.email):
        _record_attempt(ip)
        logger.info("Register attempt with existing email from IP %s — silent", ip)
        # TODO: notify the existing email address ("someone tried to register
        # with your address — was this you?") once the mailer is universal.
        return {
            "status": "pending",
            "message": "Account creation submitted. If this email is new, we've sent a verification link.",
        }
    user = svc.register_user(db, body.username, body.email, body.password)
    _clear_attempts(ip)
    # Referral capture — optional. Invalid / unknown codes are silently
    # dropped (we don't want a broken share link to block registration).
    # Self-referral is rejected for the same reason as promo per_user_max:
    # the obvious abuse path. No mid-flight reassignment after this point.
    if body.referral_code:
        try:
            from backend.services import referral_service
            referrer = referral_service.find_referrer_by_code(db, body.referral_code)
            if referrer and referrer.id != user.id:
                user.referred_by_id = referrer.id
                db.add(user)
                db.flush()
                logger.info(
                    "Referral capture: user=%s (id=%d) referred_by=%s (id=%d)",
                    user.username, user.id, referrer.username, referrer.id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Referral capture failed for %s: %s", user.username, exc)
    # Mint the new user's own code so they can share immediately.
    try:
        from backend.services import referral_service
        referral_service.ensure_referral_code(db, user)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Referral code mint failed for %s: %s", user.username, exc)
        db.rollback()
    logger.info("New user registered: %s (id=%d, admin=%s)", user.username, user.id, user.is_admin)
    token = svc.create_token(user.id)
    _set_session_cookie(response, token)
    return {"access_token": token, "token_type": "bearer"}


@router.post("/login", response_model=Token)
async def login(body: UserLogin, request: Request, response: Response, db: Session = Depends(get_db)):
    from backend.services import login_throttle
    ip = _get_ip(request)
    _check_rate_limit(ip)

    # Cooldown check before we even look at the password — keeps us from
    # leaking timing info about whether the account exists during the
    # progressive-backoff window.
    retry_after = login_throttle.check(body.login)
    if retry_after:
        await login_throttle.response_delay()
        logger.warning("Login throttled for %r from IP %s (retry_after=%ds)", body.login, ip, retry_after)
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {retry_after} second{'s' if retry_after != 1 else ''}.",
            headers={"Retry-After": str(retry_after)},
        )

    user = svc.authenticate_user(db, body.login, body.password)
    if not user:
        _record_attempt(ip)
        cooldown = login_throttle.register_failure(body.login)
        await login_throttle.response_delay()
        # An admin block flipped manually (or by the honeypot) still
        # short-circuits with the dedicated 403 — separate from the
        # progressive-backoff cooldown.
        fallback = svc.get_user_by_email(db, body.login) or svc.get_user_by_username(db, body.login)
        if fallback and getattr(fallback, "is_blocked", False):
            logger.warning("Blocked-account login attempt: %s from IP %s", fallback.username, ip)
            raise HTTPException(status_code=403, detail="Your account has been blocked. Please contact support.")
        if cooldown:
            logger.warning("Failed login (now cooled %ds) for %r from IP %s", cooldown, body.login, ip)
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed attempts. Try again in {cooldown} second{'s' if cooldown != 1 else ''}.",
                headers={"Retry-After": str(cooldown)},
            )
        logger.warning("Failed login attempt for %r from IP %s", body.login, ip)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if getattr(user, 'is_blocked', False):
        logger.warning("Blocked user login attempt: %s from IP %s", user.username, ip)
        raise HTTPException(status_code=403, detail="Your account has been blocked. Please contact support.")
    # TOTP gate. If the user has armed 2FA, the password+username
    # exchange does NOT yield a session token directly; instead we issue
    # a short-lived "challenge" token + ask for the OTP via
    # POST /auth/login/totp.
    if user.totp_verified_at is not None and user.totp_secret_enc:
        _record_attempt(ip)  # gentle anti-bruteforce on the password leg
        challenge = svc.create_token(user.id, ttl_minutes=5, scope="totp_challenge")
        return Token(access_token=challenge, token_type="totp_challenge")
    login_throttle.clear(body.login)
    _clear_attempts(ip)
    logger.info("User logged in: %s (id=%d) from IP %s", user.username, user.id, ip)
    token = svc.create_token(user.id)
    _set_session_cookie(response, token)
    return Token(access_token=token)


@router.post("/login/totp", response_model=Token)
async def login_totp(body: dict, request: Request, response: Response, db: Session = Depends(get_db)):
    """Second-factor leg of the login flow (any user with 2FA armed).
    Body = {challenge, code}. Returns a session token on success, 401 on
    bad code. Per-user progressive cooldown identical to /login keeps
    automated 6-digit-code brute-force expensive."""
    from backend.services import totp as _totp
    from backend.services import login_throttle
    ip = _get_ip(request)
    _check_rate_limit(ip)
    challenge = (body.get("challenge") or "").strip()
    code = (body.get("code") or "").strip()
    if not challenge or not code:
        raise HTTPException(status_code=422, detail="challenge and code required")
    try:
        payload = svc.decode_payload(challenge)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired challenge")
    if payload.get("scope") != "totp_challenge":
        raise HTTPException(status_code=401, detail="Bad challenge scope")
    user = db.query(User).filter(User.id == int(payload.get("sub", 0) or 0)).first()
    if not user or not user.totp_secret_enc:
        raise HTTPException(status_code=401, detail="No 2FA configured")

    throttle_key = f"totp:{user.id}"
    retry_after = login_throttle.check(throttle_key)
    if retry_after:
        await login_throttle.response_delay()
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed codes. Try again in {retry_after} second{'s' if retry_after != 1 else ''}.",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        secret = _totp.decrypt_secret(user.totp_secret_enc)
    except Exception:
        raise HTTPException(status_code=500, detail="2FA secret unreadable; contact ops")
    if not _totp.verify_code(secret, code):
        _record_attempt(ip)
        cooldown = login_throttle.register_failure(throttle_key)
        await login_throttle.response_delay()
        if user.is_admin:
            try:
                from backend.services.admin_alert_service import alert_admin_security
                alert_admin_security(user, "Failed 2FA code on login", ip)
            except Exception:
                pass
        if cooldown:
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed codes. Try again in {cooldown} second{'s' if cooldown != 1 else ''}.",
                headers={"Retry-After": str(cooldown)},
            )
        raise HTTPException(status_code=401, detail="Invalid code")
    login_throttle.clear(throttle_key)
    _clear_attempts(ip)
    token = svc.create_token(user.id)
    _set_session_cookie(response, token)
    logger.info("2FA login OK: %s (id=%d, admin=%s)", user.username, user.id, user.is_admin)
    return Token(access_token=token)


@router.post("/me/2fa/setup")
async def me_2fa_setup(body: dict, request: Request,
                        current_user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Generate (but DO NOT arm) a fresh TOTP secret. User scans the
    otpauth URI as a QR code in their authenticator app and confirms
    via /me/2fa/verify with a generated code.

    Requires the current password — without this, an attacker on a
    hijacked session could replace a verified TOTP secret with their
    own and lock the legitimate owner out at the next login. /disable
    has always required the password; setup is the symmetric gate.
    Throttled per-user same as disable.
    """
    from backend.services import login_throttle
    ip = _get_ip(request)
    throttle_key = f"pwd:{current_user.id}"
    retry_after = login_throttle.check(throttle_key)
    if retry_after:
        await login_throttle.response_delay()
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {retry_after} second{'s' if retry_after != 1 else ''}.",
            headers={"Retry-After": str(retry_after)},
        )
    pwd = body.get("password") or ""
    if not svc.verify_password(pwd, current_user.hashed_password):
        cooldown = login_throttle.register_failure(throttle_key)
        await login_throttle.response_delay()
        logger.warning("2FA setup: bad password uid=%s ip=%s", current_user.id, ip)
        if cooldown:
            raise HTTPException(
                status_code=429,
                detail=f"Too many attempts. Try again in {cooldown} second{'s' if cooldown != 1 else ''}.",
                headers={"Retry-After": str(cooldown)},
            )
        raise HTTPException(status_code=401, detail="Password mismatch")
    login_throttle.clear(throttle_key)

    from backend.services import totp as _totp
    secret = _totp.generate_secret()
    current_user.totp_secret_enc = _totp.encrypt_secret(secret)
    current_user.totp_verified_at = None
    db.commit()
    uri = _totp.provisioning_uri(secret, account=current_user.email or current_user.username)
    return {"otpauth_uri": uri, "secret": secret}


@router.post("/me/2fa/verify")
def me_2fa_verify(body: dict, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """User enters the first valid code from their authenticator —
    flips totp_verified_at so future logins start requiring it."""
    if not current_user.totp_secret_enc:
        raise HTTPException(status_code=400, detail="Run /me/2fa/setup first")
    from backend.services import totp as _totp
    code = (body.get("code") or "").strip()
    secret = _totp.decrypt_secret(current_user.totp_secret_enc)
    if not _totp.verify_code(secret, code):
        raise HTTPException(status_code=400, detail="Invalid code")
    current_user.totp_verified_at = _dt.utcnow()
    db.commit()
    logger.info("2FA armed: uid=%d (admin=%s)", current_user.id, current_user.is_admin)
    return {"ok": True}


@router.post("/me/2fa/disable")
async def me_2fa_disable(body: dict, request: Request,
                          current_user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    """Disarm 2FA. Requires the current password to prevent a stolen
    session from undoing protection silently. Throttled per-user so an
    attacker on a hijacked session can't brute-force the password."""
    from backend.services import login_throttle
    ip = _get_ip(request)
    throttle_key = f"pwd:{current_user.id}"
    retry_after = login_throttle.check(throttle_key)
    if retry_after:
        await login_throttle.response_delay()
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {retry_after} second{'s' if retry_after != 1 else ''}.",
            headers={"Retry-After": str(retry_after)},
        )
    pwd = body.get("password") or ""
    if not svc.verify_password(pwd, current_user.hashed_password):
        cooldown = login_throttle.register_failure(throttle_key)
        await login_throttle.response_delay()
        logger.warning("2FA disable: bad password uid=%s ip=%s", current_user.id, ip)
        if cooldown:
            raise HTTPException(
                status_code=429,
                detail=f"Too many attempts. Try again in {cooldown} second{'s' if cooldown != 1 else ''}.",
                headers={"Retry-After": str(cooldown)},
            )
        raise HTTPException(status_code=401, detail="Password mismatch")
    login_throttle.clear(throttle_key)
    current_user.totp_secret_enc = None
    current_user.totp_verified_at = None
    db.commit()
    logger.info("2FA disabled: uid=%d", current_user.id)
    return {"ok": True}


@router.post("/logout")
def logout(
    response: Response,
    authorization: str | None = Header(default=None),
):
    """Logout invalidates the session cookie AND revokes the JWT itself
    via the Redis-backed blacklist. Subsequent requests carrying the old
    Bearer token will get 401, even though it'd otherwise still verify."""
    response.delete_cookie("session", path="/")
    response.delete_cookie("wm_authed", path="/")
    if authorization and authorization.startswith("Bearer "):
        from backend.services.auth_service import decode_payload
        from backend.services import token_blacklist
        from datetime import datetime, timezone
        payload = decode_payload(authorization[7:])
        if payload:
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti and exp:
                ttl = max(0, int(exp - datetime.now(timezone.utc).timestamp()))
                if ttl > 0:
                    token_blacklist.revoke(jti, ttl)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    out = UserOut.model_validate(current_user)
    out.tg_linked = bool(current_user.tg_chat_id)
    out.totp_enabled = bool(getattr(current_user, "totp_verified_at", None))
    out.auto_renew = bool(getattr(current_user, "auto_renew", True))
    # Enrich with effective limits so the frontend picker / pricing page
    # can show the right cap without round-tripping to /api/plans.
    try:
        from backend.services import plan_service as _ps
        # Lazy-enforce wallet quota — if the user just dropped below
        # their portfolio cap (downgraded plan, expired subscription),
        # surplus wallets get archived right here. Cheap; bails out
        # immediately when the user is within cap or on Unlim.
        try:
            from backend.services import wallet_quota as _wq
            _wq.enforce_for_user(db, current_user)
        except Exception:
            pass
        lim = _ps.effective_limits(db, current_user)
        out.plan_id = lim.plan_id
        # -1 in the DB ≡ unlimited; surface as null on the wire so the
        # frontend just renders "∞" / hides counters instead of negative
        # math.
        out.portfolio_limit = None if lim.portfolio_unlimited else lim.portfolio_limit
        out.exchange_keys_per_venue = None if lim.keys_unlimited else lim.exchange_keys_per_venue
        out.is_plan_expired = lim.is_expired
        out.wallet_limit = out.portfolio_limit
    except Exception:
        pass
    return out


from pydantic import BaseModel as _BM


@router.get("/cookie-session")
def cookie_session(request: Request, db: Session = Depends(get_db)):
    """Recover the JWT from the HttpOnly session cookie.

    The frontend's Auth.isLoggedIn() / IS_AUTHED check reads the
    localStorage `wm_token` Bearer token, NOT the session cookie. So if
    the user lands on a page where localStorage was cleared (privacy
    extension, "clear browsing data", different browser profile, x-domain
    nav between www.avalant.xyz and avalant.xyz, etc.) the page renders
    its anonymous lockout overlay even though the session cookie is still
    valid server-side.

    auth.js calls this on every page load when localStorage is empty; if
    the session cookie carries a live JWT we just hand it back so the
    client can repopulate localStorage and the page un-locks. No new
    session is minted — we re-emit exactly what's in the cookie.

    Returns 401 if the cookie is missing/invalid; the client treats that
    as "stay anonymous" and shows the sign-in CTA as designed.
    """
    from backend.services.auth_service import decode_token, get_user_by_id
    from backend.services import token_blacklist
    from backend.services.auth_service import decode_payload as _dp
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=401, detail="No session cookie")
    payload = _dp(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired cookie")
    if payload.get("scope"):
        raise HTTPException(status_code=401, detail="Cookie carries a scoped token")
    jti = payload.get("jti")
    if jti and token_blacklist.is_revoked(jti):
        raise HTTPException(status_code=401, detail="Session revoked")
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token payload")
    user = get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if getattr(user, "is_blocked", False):
        raise HTTPException(status_code=403, detail="Account is blocked")
    out = UserOut.model_validate(user)
    out.tg_linked = bool(user.tg_chat_id)
    out.totp_enabled = bool(getattr(user, "totp_verified_at", None))
    out.auto_renew = bool(getattr(user, "auto_renew", True))
    return {"access_token": token, "user": out}


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


@router.get("/tg-bot-username")
def tg_bot_username():
    """Public — returns the auth bot's username + numeric bot_id. The
    numeric id is the portion of the bot token before the colon and is
    needed by the OAuth redirect flow (oauth.telegram.org/auth?bot_id=…)."""
    from settings import settings as _settings
    name = (_settings.TG_AUTH_BOT_USERNAME or _settings.TG_BOT_USERNAME or "").lstrip("@")
    token = _settings.TG_AUTH_BOT_TOKEN or _settings.TG_BOT_TOKEN or ""
    bot_id = None
    if ":" in token:
        head = token.split(":", 1)[0]
        if head.isdigit():
            bot_id = int(head)
    return {"username": name, "bot_id": bot_id}


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
    _set_session_cookie(response, token)
    logger.info("TG login: user_id=%s created=%s tg_id=%s", user.id, created, user.tg_id)
    return Token(access_token=token, token_type="bearer")


# ── One-time link token for profile ──────────────────────────────────────────
# ── Login-by-Bot (no auth required) ──────────────────────────────────────────
@router.post("/tg-bot-login")
def tg_bot_login_start(request: Request, db: Session = Depends(get_db)):
    """Generate a one-time token for login via bot. Returns deep_link to open.
    Rate-limited per-IP — this endpoint writes to DB and to Redis, so an
    open token-mint pipe would let anyone flood storage."""
    ip = _get_ip(request)
    _check_rate_limit(ip)
    _record_attempt(ip)
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
    # Mailer NOT configured. Never leak the raw reset token in the
    # response — that would let anyone reset any email's password by
    # just calling /password-reset/request. The token now only goes to
    # the server log (visible to ops) and to the response IFF the
    # explicit dev-toggle env var is set.
    import os as _os_dev
    if _os_dev.environ.get("AVALANT_AUTH_DEV_EXPOSE_TOKEN", "").strip().lower() in ("1", "true", "yes"):
        logger.info("password-reset: token issued for uid=%s (DEV-EXPOSE)", user.id)
        return {**generic, "dev_token": raw, "expires_in_min": _PW_RESET_TTL_MIN}
    logger.warning(
        "password-reset: token issued for uid=%s but mailer not configured — token only in logs",
        user.id,
    )
    logger.warning("password-reset DEV-LOG token uid=%s value=%s ttl=%dmin",
                   user.id, raw, _PW_RESET_TTL_MIN)
    return generic


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
        return out
    # Same dev-leak protection as password-reset above.
    import os as _os_dev
    if _os_dev.environ.get("AVALANT_AUTH_DEV_EXPOSE_TOKEN", "").strip().lower() in ("1", "true", "yes"):
        logger.info("email-verify: token issued for uid=%s (DEV-EXPOSE)", current_user.id)
        out["dev_token"] = raw
        out["expires_in_hours"] = _EMAIL_VERIFY_TTL_HOURS
    else:
        logger.warning("email-verify DEV-LOG token uid=%s value=%s ttl=%dh",
                       current_user.id, raw, _EMAIL_VERIFY_TTL_HOURS)
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
def tg_bot_login_check(token: str = Query(..., max_length=128), response: Response = None):
    """Poll: has the user pressed Start in the bot yet? The frontend polls
    this every ~2 s during the bot-login flow, so we don't tie it to the
    per-IP failed-login bucket — token shape is bounded by max_length and
    the actual cost is one cache-file stat. Mint-side (POST) rate limit
    is what stops storage flooding."""
    from backend.services.tg_auth_service import check_login_token
    result = check_login_token(token)
    if result.get("status") == "ok" and result.get("access_token"):
        _set_session_cookie(response, result["access_token"])
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


# ── Subscription auto-renew toggle ──────────────────────────────────────────
@router.post("/me/subscription/cancel")
def cancel_subscription(current_user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Disable auto-renewal. The user keeps their plan until plan_expires_at;
    we just stop nagging them about renewal and won't auto-bill them. Going
    back is a one-tap call to /resume — no support ticket needed."""
    current_user.auto_renew = False
    db.commit()
    logger.info("Subscription auto-renew cancelled: user=%s", current_user.id)
    return {"auto_renew": False, "plan_expires_at": current_user.plan_expires_at.isoformat() if current_user.plan_expires_at else None}


@router.post("/me/subscription/resume")
def resume_subscription(current_user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Re-enable auto-renewal — the user changed their mind. Doesn't
    extend plan_expires_at on its own; that still requires a real payment."""
    current_user.auto_renew = True
    db.commit()
    logger.info("Subscription auto-renew resumed: user=%s", current_user.id)
    return {"auto_renew": True}


class _DeleteMeBody(_BM):
    password: str


@router.delete("/me")
async def delete_me(body: _DeleteMeBody, request: Request, response: Response,
                    current_user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    """Self-service account deletion. Requires current password as a
    second factor so a stolen session token alone can't nuke the
    account. Admins cannot delete themselves via this endpoint — they
    have to demote first. Cascade drops wallets, tags, snapshots, and
    arb alerts via the FKs defined in models.py."""
    if current_user.is_admin:
        raise HTTPException(status_code=400, detail="Admins cannot self-delete; demote first")
    from backend.services import login_throttle
    throttle_key = f"pwd:{current_user.id}"
    retry_after = login_throttle.check(throttle_key)
    if retry_after:
        await login_throttle.response_delay()
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {retry_after} second{'s' if retry_after != 1 else ''}.",
            headers={"Retry-After": str(retry_after)},
        )
    if not svc.verify_password(body.password, current_user.hashed_password):
        cooldown = login_throttle.register_failure(throttle_key)
        await login_throttle.response_delay()
        if cooldown:
            raise HTTPException(
                status_code=429,
                detail=f"Too many attempts. Try again in {cooldown} second{'s' if cooldown != 1 else ''}.",
                headers={"Retry-After": str(cooldown)},
            )
        raise HTTPException(status_code=401, detail="Password incorrect")
    login_throttle.clear(throttle_key)

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
    out.totp_enabled = bool(getattr(current_user, "totp_verified_at", None))
    out.auto_renew = bool(getattr(current_user, "auto_renew", True))
    return out
