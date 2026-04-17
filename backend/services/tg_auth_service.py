"""Telegram login + one-time link tokens.

Two flows:

1) Login widget (https://core.telegram.org/widgets/login)
   Client receives signed payload {id, first_name, last_name, username, photo_url,
   auth_date, hash}. Server verifies HMAC(sha256(BOT_TOKEN), data_check_string).
   Auth_date must be within WIDGET_MAX_AGE seconds. On success: find User by
   tg_id, or create a new one (username = "tg_<id>", random bcrypt-hashed
   password since this account has no real password — can only login via TG).

2) Link-by-Start (profile linking)
   Logged-in user POSTs /auth/me/tg-link-token → server generates a 32-byte
   random token, stores its sha256 in DB with 15-min expiry, returns
   {deep_link: "https://t.me/avalant_bot?start=link-<token>"}.
   Bot's /start handler matches args "link-<TOKEN>", looks up the hash,
   single-use consume, sets users.tg_chat_id + tg_id + tg_username.

Security:
- Widget hash verified server-side with constant-time compare
- Widget stale after WIDGET_MAX_AGE (default 1 h) — rejects replay
- Link tokens: 256-bit entropy, sha256-hashed at rest, single-use, 15 min TTL,
  auto-prune on every issue call
- Bot_token sha256 cached at module load; never logged
- All input typed strictly; raises ValueError on bad data
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from backend.db.models import TgLinkToken, User
from backend.services.auth_service import create_token
from settings import settings

logger = logging.getLogger("avalant.tg_auth")

WIDGET_MAX_AGE = 3600         # seconds — widget auth payload must be newer than this
LINK_TOKEN_TTL = 15 * 60      # seconds — deep-link token validity
MAX_OUTSTANDING_PER_USER = 5  # prune to this count per user on issue


# ── Widget hash verification ─────────────────────────────────────────────────

def _bot_secret_key() -> bytes:
    """Widget signs with sha256(bot_token) as the HMAC secret."""
    token = (settings.TG_BOT_TOKEN or "").encode()
    if not token:
        raise ValueError("TG_BOT_TOKEN not configured")
    return hashlib.sha256(token).digest()


def verify_widget_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Verify Telegram login widget payload. Returns normalised dict or raises ValueError.
    Expected fields: id (int), first_name (str), last_name, username, photo_url,
    auth_date (int), hash (hex str)."""
    if not isinstance(data, dict):
        raise ValueError("payload must be an object")

    received_hash = str(data.get("hash") or "")
    if not received_hash:
        raise ValueError("missing hash")

    # Copy without the hash for computing data_check_string
    pairs = {k: v for k, v in data.items() if k != "hash" and v is not None and v != ""}

    required = ("id", "auth_date")
    for k in required:
        if k not in pairs:
            raise ValueError(f"missing field: {k}")

    try:
        tg_id = int(pairs["id"])
        auth_date = int(pairs["auth_date"])
    except (TypeError, ValueError):
        raise ValueError("id / auth_date must be integers")

    if auth_date <= 0 or tg_id <= 0:
        raise ValueError("invalid auth_date or id")

    now = int(time.time())
    if abs(now - auth_date) > WIDGET_MAX_AGE:
        raise ValueError("auth_date is stale (> 1 hour old)")

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    expected_hash = hmac.new(_bot_secret_key(), data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise ValueError("hash mismatch")

    return {
        "tg_id": tg_id,
        "first_name": str(pairs.get("first_name") or "").strip()[:64],
        "last_name":  str(pairs.get("last_name")  or "").strip()[:64],
        "username":   (str(pairs.get("username")) or None) if pairs.get("username") else None,
        "photo_url":  str(pairs.get("photo_url") or "") or None,
        "auth_date":  auth_date,
    }


# ── User resolution from widget ──────────────────────────────────────────────

def _make_unique_username(db: Session, base: str) -> str:
    """Return a username not taken. Falls back to base + random suffix."""
    candidate = base
    for _ in range(5):
        if not db.query(User).filter(User.username == candidate).first():
            return candidate
        candidate = base + "_" + secrets.token_hex(3)
    return base + "_" + secrets.token_hex(4)


def find_or_create_user_from_widget(db: Session, payload: dict[str, Any]) -> tuple[User, bool]:
    """Look up User by tg_id; if missing, create a shell account. Returns (user, created)."""
    tg_id = int(payload["tg_id"])
    user = db.query(User).filter(User.tg_id == tg_id).first()
    if user:
        # Refresh username if Telegram has one now and we don't
        if not user.tg_username and payload.get("username"):
            user.tg_username = payload["username"]
            db.commit()
        return user, False

    # Create shell account — no password login is possible; must login via TG
    tg_username = (payload.get("username") or "").strip()
    base = (tg_username or f"tg_{tg_id}").lower()
    # Sanitise: keep alphanumerics + underscore, trim to 24
    import re
    base = re.sub(r"[^a-z0-9_]", "_", base)[:24] or f"tg_{tg_id}"
    username = _make_unique_username(db, base)

    email = f"tg_{tg_id}@tg.avalant.local"  # placeholder; guarantees uniqueness
    # Unusable password hash — bcrypt of random bytes; user must login via widget
    from passlib.hash import bcrypt
    random_pw = secrets.token_urlsafe(32)
    hashed = bcrypt.using(rounds=12).hash(random_pw)

    user = User(
        username=username,
        email=email,
        hashed_password=hashed,
        tg_id=tg_id,
        tg_username=tg_username or None,
    )
    # First user rule (same as register_user): promote to admin + unlim
    user_count = db.query(User).count()
    if user_count == 0:
        user.is_admin = True
        user.plan = "unlim"

    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("Created user from TG widget: id=%s tg_id=%s username=%s", user.id, tg_id, username)
    return user, True


def login_via_widget(db: Session, widget_data: dict[str, Any]) -> tuple[User, str, bool]:
    """End-to-end: verify + resolve + mint JWT. Returns (user, jwt, created)."""
    payload = verify_widget_payload(widget_data)
    user, created = find_or_create_user_from_widget(db, payload)
    if user.is_blocked:
        raise ValueError("User is blocked")
    token = create_token(user.id)
    return user, token, created


# ── One-time link tokens ─────────────────────────────────────────────────────

def _hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def issue_link_token(db: Session, user_id: int) -> dict[str, Any]:
    """Generate a fresh single-use token and return the deep link."""
    # Prune stale tokens for this user (keep DB small + limit outstanding)
    _prune_user_tokens(db, user_id)

    raw = secrets.token_urlsafe(24)  # ~32 bytes of entropy
    row = TgLinkToken(
        user_id=user_id,
        token_hash=_hash_token(raw),
        expires_at=datetime.utcnow() + timedelta(seconds=LINK_TOKEN_TTL),
    )
    db.add(row)
    db.commit()

    bot_username = settings.TG_BOT_USERNAME or "avalant_bot"
    return {
        "token": raw,  # return to caller; never stored plain anywhere else
        "deep_link": f"https://t.me/{bot_username}?start=link-{raw}",
        "expires_in_sec": LINK_TOKEN_TTL,
    }


def _prune_user_tokens(db: Session, user_id: int) -> None:
    """Delete expired + over-limit tokens for this user."""
    now = datetime.utcnow()
    db.query(TgLinkToken).filter(
        TgLinkToken.user_id == user_id,
        TgLinkToken.expires_at < now,
    ).delete()
    # Keep at most MAX_OUTSTANDING_PER_USER unused active tokens
    active = (
        db.query(TgLinkToken)
        .filter(TgLinkToken.user_id == user_id, TgLinkToken.used_at.is_(None))
        .order_by(TgLinkToken.created_at.desc())
        .all()
    )
    if len(active) >= MAX_OUTSTANDING_PER_USER:
        for extra in active[MAX_OUTSTANDING_PER_USER - 1:]:
            db.delete(extra)
    db.commit()


def consume_link_token(db: Session, token: str, tg_id: int, tg_chat_id: int,
                       tg_username: str | None) -> User | None:
    """Called by the bot when it sees /start link-<token>. Returns the User
    with tg_chat_id + tg_id set, or None if token invalid/expired/used/collision."""
    if not token or len(token) < 8:
        return None
    row = (
        db.query(TgLinkToken)
        .filter(TgLinkToken.token_hash == _hash_token(token))
        .first()
    )
    if row is None:
        return None
    if row.used_at is not None:
        return None
    if row.expires_at < datetime.utcnow():
        return None

    user = db.query(User).filter(User.id == row.user_id).first()
    if user is None or user.is_blocked:
        return None

    # Guard against another Avalant account already tied to this tg_id
    conflict = db.query(User).filter(User.tg_id == tg_id, User.id != user.id).first()
    if conflict is not None:
        logger.warning("Link collision: tg_id=%s already tied to user_id=%s (attempt %s)",
                       tg_id, conflict.id, user.id)
        # Mark token consumed so it can't be retried
        row.used_at = datetime.utcnow()
        db.commit()
        return None

    user.tg_id = tg_id
    user.tg_chat_id = tg_chat_id
    if tg_username and not user.tg_username:
        user.tg_username = tg_username
    row.used_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    logger.info("TG linked via token: user_id=%s tg_id=%s chat=%s", user.id, tg_id, tg_chat_id)
    return user


# ── Login-by-Bot tokens (no auth required) ───────────────────────────────────
# In-memory store for pending login results: token_hash → {jwt, user_id, ts}
# Cleaned up after 5 minutes. NOT in DB — ephemeral by design.
_login_results: dict[str, dict] = {}
_LOGIN_TOKEN_TTL = 300  # 5 min


def issue_login_token(db: Session) -> dict[str, Any]:
    """Generate a token for unauthenticated login-by-bot flow.
    Returns {token, deep_link, expires_in_sec}. No user_id needed — the bot
    will resolve the user from tg_id when they press Start."""
    raw = secrets.token_urlsafe(24)
    h = _hash_token(raw)
    _login_results[h] = {"status": "pending", "ts": time.time()}
    # Prune old entries
    now = time.time()
    stale = [k for k, v in _login_results.items() if now - v["ts"] > _LOGIN_TOKEN_TTL]
    for k in stale:
        _login_results.pop(k, None)

    bot_username = settings.TG_BOT_USERNAME or "avalant_bot"
    return {
        "token": raw,
        "deep_link": f"https://t.me/{bot_username}?start=auth-{raw}",
        "expires_in_sec": _LOGIN_TOKEN_TTL,
    }


def consume_login_token(db: Session, token: str, tg_id: int, tg_chat_id: int,
                        tg_username: str | None, first_name: str | None) -> str | None:
    """Called by the bot when it sees /start auth-<token>.
    Finds or creates user by tg_id, mints JWT, stores in _login_results.
    Returns the bot reply message or None."""
    h = _hash_token(token)
    entry = _login_results.get(h)
    if not entry or entry.get("status") != "pending":
        return None
    if time.time() - entry["ts"] > _LOGIN_TOKEN_TTL:
        _login_results.pop(h, None)
        return None

    # Find or create user
    payload = {
        "tg_id": tg_id,
        "username": tg_username,
        "first_name": first_name or "",
    }
    user, created = find_or_create_user_from_widget(db, payload)
    if user.is_blocked:
        _login_results[h] = {"status": "blocked", "ts": entry["ts"]}
        return "Your account is blocked."

    # Set chat_id if missing
    if not user.tg_chat_id:
        user.tg_chat_id = tg_chat_id
    if not user.tg_id:
        user.tg_id = tg_id
    if tg_username and not user.tg_username:
        user.tg_username = tg_username
    db.commit()

    jwt = create_token(user.id)
    _login_results[h] = {
        "status": "ok",
        "jwt": jwt,
        "user_id": user.id,
        "username": user.username,
        "ts": entry["ts"],
    }
    action = "created" if created else "logged in"
    logger.info("TG login-by-bot: user_id=%s %s tg_id=%s", user.id, action, tg_id)
    return f"✅ {action.title()}! You can close this chat and return to Avalant."


def check_login_token(token: str) -> dict:
    """Poll endpoint: returns {status: pending|ok|expired, jwt?, user?}."""
    h = _hash_token(token)
    entry = _login_results.get(h)
    if not entry:
        return {"status": "expired"}
    if time.time() - entry["ts"] > _LOGIN_TOKEN_TTL:
        _login_results.pop(h, None)
        return {"status": "expired"}
    if entry.get("status") == "ok":
        # One-time read — remove after delivery
        _login_results.pop(h, None)
        return {
            "status": "ok",
            "access_token": entry["jwt"],
            "user_id": entry["user_id"],
            "username": entry.get("username"),
        }
    return {"status": entry.get("status", "pending")}
    db.refresh(user)
    logger.info("TG linked via token: user_id=%s tg_id=%s chat=%s", user.id, tg_id, tg_chat_id)
    return user
