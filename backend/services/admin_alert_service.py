"""Admin-only Telegram notifications.

Routes through the auth bot (TG_AUTH_BOT_TOKEN) when configured, otherwise
falls back to the alerts bot (TG_BOT_TOKEN). Recipients are every user with
is_admin=True AND tg_chat_id set — admins who haven't linked their Telegram
account silently get nothing (no error path, alerting is best-effort).

Used for:
  · payment events (new paid subscription, refund)
  · security signals (admin TOTP failures, blocked-user login attempts)
  · operational events (deploy, migration, runaway error rate)

Anti-spam: an in-memory dedup window (60 s by default) drops repeats of the
same exact message body so a flapping system can't fan out 60 messages a
minute. Dedup is per-process — across replicas the worst case is each
replica sends its own copy once, which is acceptable.

This is NEVER awaited from a hot request path — fire-and-forget via
asyncio.create_task, otherwise a slow Telegram API will stall whatever
called it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

import httpx
from sqlalchemy.orm import Session

from backend.db.base import SessionLocal
from backend.db.models import User
from settings import settings

logger = logging.getLogger("avalant.admin_alert")

_DEDUP_WINDOW_S = 60.0
_recent: dict[str, float] = {}


def _bot_token() -> str:
    """Auth bot first; alerts bot only if no auth bot is set. We deliberately
    prefer the auth bot since admins use it for login + system events — keeps
    the user-facing alerts firehose separate."""
    return (settings.TG_AUTH_BOT_TOKEN or settings.TG_BOT_TOKEN or "").strip()


def _admin_chat_ids(db: Session) -> list[int]:
    """Every linked admin's chat_id. Ignores admins without a Telegram link
    rather than erroring — alerting is best-effort."""
    rows = (
        db.query(User.tg_chat_id)
        .filter(User.is_admin.is_(True))
        .filter(User.tg_chat_id.isnot(None))
        .all()
    )
    return [r[0] for r in rows if r[0]]


async def _send_one(token: str, chat_id: int, text: str, parse_mode: str | None) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=payload)
            j = r.json()
            if not j.get("ok"):
                logger.debug("admin alert sendMessage not ok: %s", j)
    except Exception as exc:
        logger.debug("admin alert send failed: %s", exc)


def _should_send(text: str) -> bool:
    """Drop dupes within the dedup window. Cheap GC of stale entries."""
    now = time.time()
    last = _recent.get(text, 0.0)
    if now - last < _DEDUP_WINDOW_S:
        return False
    _recent[text] = now
    if len(_recent) > 256:
        cutoff = now - _DEDUP_WINDOW_S
        for k, ts in list(_recent.items()):
            if ts < cutoff:
                _recent.pop(k, None)
    return True


async def _notify(text: str, parse_mode: str | None) -> None:
    token = _bot_token()
    if not token:
        return
    if not _should_send(text):
        return
    db = SessionLocal()
    try:
        chats = _admin_chat_ids(db)
    finally:
        db.close()
    if not chats:
        return
    await asyncio.gather(*(_send_one(token, c, text, parse_mode) for c in chats),
                         return_exceptions=True)


def notify_admins(text: str, *, parse_mode: str | None = "HTML") -> None:
    """Fire-and-forget admin broadcast. Safe from any context — sync or async,
    request handler or background task. Does nothing if no auth/alerts bot
    token is configured or no admin has linked Telegram."""
    if not text:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(_notify(text, parse_mode))
        return
    # No running loop (e.g. called from a sync trade adapter or during
    # startup). Fire on a fresh loop in a daemon thread so we never block
    # the caller. Cheap because we only do this off the hot path.
    import threading
    def _runner():
        try:
            asyncio.run(_notify(text, parse_mode))
        except Exception:
            pass
    threading.Thread(target=_runner, daemon=True).start()


# ── Convenience helpers (callers don't need to format messages by hand) ──────

def alert_payment(user: User, plan_slug: str, amount_usd: float) -> None:
    notify_admins(
        f"💳 <b>Payment received</b>\n"
        f"User: <code>{user.username}</code> (#{user.id})\n"
        f"Plan: <b>{plan_slug}</b>\n"
        f"Amount: <b>${amount_usd:.2f}</b>"
    )


def alert_admin_security(user: User, event: str, ip: str | None = None) -> None:
    notify_admins(
        f"🛡 <b>Admin security event</b>\n"
        f"User: <code>{user.username}</code> (#{user.id})\n"
        f"Event: {event}\n"
        f"IP: <code>{ip or '?'}</code>"
    )


def alert_user_blocked(user: User, reason: str) -> None:
    notify_admins(
        f"⛔ <b>User blocked</b>\n"
        f"<code>{user.username}</code> (#{user.id}) — {reason}"
    )
