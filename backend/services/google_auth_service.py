"""Google OAuth 2.0 login flow.

Wire-up (set on every web replica):
    GOOGLE_OAUTH_CLIENT_ID=<client_id from Google Cloud Console>
    GOOGLE_OAUTH_CLIENT_SECRET=<client_secret>
    GOOGLE_OAUTH_REDIRECT_URI=https://avalant.xyz/api/auth/google/callback

Flow:
    1. User clicks "Sign in with Google" → frontend hits /api/auth/google/authorize
    2. We return a redirect to Google's consent page with a CSRF `state` param
       (JWT-signed, 5min TTL).
    3. Google bounces back to /api/auth/google/callback?code=...&state=...
    4. We verify state, exchange `code` for an id_token, parse the email,
       find-or-create the matching user, set the session cookie, redirect to
       /portfolio (or wherever the original request asked).

Email match logic:
    - id_token.email + email_verified are the source of truth (Google asserts).
    - If a user already exists with that email (registered by password earlier),
      we silently log them in — the verified Google email proves ownership.
    - If no user matches, we create one with username = local-part (deduped if
      needed) and no password. The user can set a password later via /profile.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
from typing import Any

import httpx
from sqlalchemy.orm import Session

from backend.db.models import User
import backend.services.auth_service as svc

logger = logging.getLogger("avalant.auth.google")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

_DEFAULT_SCOPES = "openid email profile"


def _client_id() -> str | None:
    return (os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip() or None


def _client_secret() -> str | None:
    return (os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip() or None


def _redirect_uri() -> str:
    explicit = (os.environ.get("GOOGLE_OAUTH_REDIRECT_URI") or "").strip()
    if explicit:
        return explicit
    base = (os.environ.get("PUBLIC_BASE_URL") or "https://avalant.xyz").rstrip("/")
    return f"{base}/api/auth/google/callback"


def is_configured() -> bool:
    return bool(_client_id() and _client_secret())


def build_authorize_url(next_path: str = "/portfolio") -> str:
    """Compose the Google consent URL. CSRF `state` is a signed token with
    the user-requested redirect packed in, so we can deliver them back to
    where they were going after sign-in."""
    if not is_configured():
        raise RuntimeError("Google OAuth is not configured on this server")
    # Pack next_path into state. JWT signed with SECRET_KEY so we can recover
    # it on callback without trusting the client.
    safe_next = next_path if (next_path or "").startswith("/") else "/portfolio"
    state = svc.create_token(
        # Reuse the JWT helper; use a synthetic uid=0 + scope+next in claims.
        # auth_service.create_token signs the dict claims for us.
        0, ttl_minutes=5, scope="google_oauth_state", extra={"next": safe_next}
    )
    from urllib.parse import urlencode
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": _DEFAULT_SCOPES,
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _decode_state(state: str) -> dict[str, Any]:
    payload = svc.decode_payload(state)
    if payload.get("scope") != "google_oauth_state":
        raise ValueError("bad state scope")
    return payload


def exchange_code(code: str) -> dict[str, Any]:
    """Exchange the OAuth code for tokens, return the userinfo payload
    (email, name, picture, sub)."""
    if not is_configured():
        raise RuntimeError("Google OAuth not configured")
    with httpx.Client(timeout=10.0) as c:
        r = c.post(TOKEN_URL, data={
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code",
        })
    if r.status_code >= 400:
        logger.warning("google token exchange failed: %s %s", r.status_code, r.text[:200])
        raise ValueError("Google token exchange failed")
    tok = r.json()
    access_token = tok.get("access_token")
    if not access_token:
        raise ValueError("Google did not return access_token")
    # Pull userinfo (cleaner than parsing id_token JWT, and we already have
    # an OAuth-scope-protected access_token).
    with httpx.Client(timeout=10.0) as c:
        ur = c.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
    if ur.status_code >= 400:
        logger.warning("google userinfo failed: %s %s", ur.status_code, ur.text[:200])
        raise ValueError("Google userinfo failed")
    info = ur.json()
    email = (info.get("email") or "").lower().strip()
    if not email:
        raise ValueError("Google did not provide email")
    if not info.get("email_verified"):
        raise ValueError("Google email is not verified")
    return info


def _safe_username(email: str) -> str:
    """Derive a default username from an email's local-part — strip
    everything but [a-z0-9_-], min 3 chars."""
    local = email.split("@", 1)[0].lower()
    base = re.sub(r"[^a-z0-9_-]+", "", local) or "user"
    if len(base) < 3:
        base = (base + "user")[:8]
    return base[:32]


def _dedupe_username(db: Session, base: str) -> str:
    """Find an unused username starting from `base`. Tries base, base2, base3..."""
    cand = base
    suffix = 2
    while svc.get_user_by_username(db, cand):
        cand = f"{base}{suffix}"
        suffix += 1
        if suffix > 200:
            cand = f"{base}_{secrets.token_hex(3)}"
            break
    return cand


def login_or_register_via_google(db: Session, info: dict) -> tuple[User, str, bool]:
    """Find the user by email (case-insensitive) or create one with no
    password. Returns (user, session_token, created_flag)."""
    email = info["email"].lower().strip()
    user = svc.get_user_by_email(db, email)
    created = False
    if user is None:
        username = _dedupe_username(db, _safe_username(email))
        # No password — the user signs in via Google. They can set a local
        # password later via the password-reset flow.
        random_pw = secrets.token_urlsafe(32)
        user = svc.register_user(db, username, email, random_pw)
        # Mark email as verified — Google already vouched for it.
        from datetime import datetime
        user.email_verified_at = datetime.utcnow()
        # Mint a referral code so the new user can share immediately.
        try:
            from backend.services import referral_service
            referral_service.ensure_referral_code(db, user)
        except Exception:  # noqa: BLE001
            pass
        db.commit()
        created = True
        logger.info("Google OAuth: new user uid=%s email=%s username=%s",
                    user.id, email, user.username)
    else:
        # Existing user — silent log-in. If their email_verified_at is NULL
        # (legacy user), set it now since Google has just verified.
        if not user.email_verified_at:
            from datetime import datetime
            user.email_verified_at = datetime.utcnow()
            db.commit()
        logger.info("Google OAuth: existing user uid=%s email=%s", user.id, email)
    token = svc.create_token(user.id)
    return user, token, created
