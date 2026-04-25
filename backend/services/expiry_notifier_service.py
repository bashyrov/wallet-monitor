"""Subscription-expiry reminder daemon.

Routes through the auth bot (TG_AUTH_BOT_TOKEN, falls back to TG_BOT_TOKEN
if no auth bot is configured — same precedence as admin_alert_service).
Each reminder embeds a /pricing deep-link so the user can renew with two
taps from their phone.

Eligibility per user:
  · auto_renew is True (user hasn't clicked Cancel)
  · plan_expires_at is within `expiry_notice_days` from now
  · plan_expires_at is in the future (we don't ping after expiry)
  · tg_chat_id is set (user has linked TG, otherwise we can't reach them)
  · expiry_notice_last_sent_at is null OR
    now - expiry_notice_last_sent_at >= expiry_notice_interval_hours

Throttle is per-user via expiry_notice_last_sent_at — a daemon restart
won't double-fire because we read+update that column inside the same
transaction as the send.

Cadence: the loop wakes every 30 minutes and scans the entire candidate
set. With small user counts that's pointless overhead but safe; we'll
move to a Redis-backed schedule if the table grows past ~10k.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from backend.db.base import SessionLocal
from backend.db.models import User
from backend.services import admin_settings
from settings import settings

logger = logging.getLogger("avalant.expiry_notifier")

_LOOP_INTERVAL_S = 30 * 60  # scan every 30 min
_thread: threading.Thread | None = None
_stop = threading.Event()


def _bot_token() -> str:
    """Same precedence as admin_alert_service: auth bot first, alerts bot
    second, empty string if neither is set (the daemon then no-ops)."""
    return (settings.TG_AUTH_BOT_TOKEN or settings.TG_BOT_TOKEN or "").strip()


def _public_base() -> str:
    return (settings.PUBLIC_BASE_URL or "https://avalant.xyz").rstrip("/")


def _format_message(user: User, days_left: int) -> tuple[str, dict]:
    """Build the message body + an inline-keyboard with a Renew button.
    days_left is the integer ceil of the time-to-expiry — phrased as 'today'
    when ≤0, otherwise '<n> day(s)'."""
    if days_left <= 0:
        when = "today"
    elif days_left == 1:
        when = "tomorrow"
    else:
        when = f"in <b>{days_left} days</b>"
    text = (
        f"⏰ <b>Subscription reminder</b>\n\n"
        f"Hi {(user.username or 'there')} — your <b>{user.plan or '?'}</b> plan "
        f"expires {when}. Tap below to renew or change tier.\n\n"
        f"You can stop these reminders from your <b>Profile → Cancel renewal</b>."
    )
    base = _public_base()
    kb = {
        "inline_keyboard": [[
            {"text": "🔁 Renew on Avalant", "url": f"{base}/pricing?renew=1"}
        ]]
    }
    return text, kb


async def _send(token: str, chat_id: int, text: str, reply_markup: dict) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": reply_markup,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=payload)
            j = r.json()
            return bool(j.get("ok"))
    except Exception as exc:
        logger.debug("expiry notify send failed: %s", exc)
        return False


def _candidates(db, days_window: int, interval_hours: int) -> list[User]:
    """Pull every user eligible right now. Cheap scan — runs every 30 min,
    table is small."""
    now = datetime.utcnow()
    cutoff = now + timedelta(days=days_window)
    interval = timedelta(hours=interval_hours)
    rows = (
        db.query(User)
        .filter(User.auto_renew.is_(True))
        .filter(User.tg_chat_id.isnot(None))
        .filter(User.plan_expires_at.isnot(None))
        .filter(User.plan_expires_at <= cutoff)
        .filter(User.plan_expires_at > now)
        .all()
    )
    out: list[User] = []
    for u in rows:
        last = u.expiry_notice_last_sent_at
        if last is None or (now - last) >= interval:
            out.append(u)
    return out


async def _tick_async() -> None:
    """One scan-and-send pass. Inside its own asyncio loop so the worker
    thread doesn't depend on uvicorn's loop."""
    days = admin_settings.get_expiry_notice_days()
    if days <= 0:
        return
    interval_h = admin_settings.get_expiry_notice_interval_hours()
    token = _bot_token()
    if not token:
        return

    db = SessionLocal()
    try:
        users = _candidates(db, days, interval_h)
        if not users:
            return
        now = datetime.utcnow()
        sent = 0
        for u in users:
            ttl = u.plan_expires_at - now
            days_left = max(0, int(ttl.total_seconds() // 86400) + (1 if ttl.total_seconds() % 86400 else 0))
            text, kb = _format_message(u, days_left)
            ok = await _send(token, int(u.tg_chat_id), text, kb)
            if ok:
                u.expiry_notice_last_sent_at = now
                sent += 1
        db.commit()
        if sent:
            logger.info("expiry notifier: pinged %d / %d eligible users", sent, len(users))
    finally:
        db.close()


def _tick() -> None:
    try:
        asyncio.run(_tick_async())
    except Exception as exc:
        logger.warning("expiry notifier tick failed: %s", exc)


def _loop() -> None:
    logger.info("expiry-notifier started (interval %ds)", _LOOP_INTERVAL_S)
    while not _stop.is_set():
        _tick()
        # Sleep in chunks so a stop signal during the long sleep is responsive.
        slept = 0.0
        while slept < _LOOP_INTERVAL_S and not _stop.is_set():
            time.sleep(min(2.0, _LOOP_INTERVAL_S - slept))
            slept += 2.0


def start_expiry_notifier() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    if not _bot_token():
        logger.info("expiry-notifier: no TG bot token configured — disabled")
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="expiry-notifier", daemon=True)
    _thread.start()


def stop_expiry_notifier() -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5.0)
