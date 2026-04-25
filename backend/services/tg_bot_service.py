"""Telegram bot long-polling — links user chats to Avalant accounts.

Users can't receive bot messages by @username alone — Telegram requires a numeric
chat_id, obtainable only after the user initiates the dialog with the bot. This
service long-polls getUpdates; on /start it looks up the Avalant user by the
sender's @username and stores the chat_id on that user row.

Two-bot mode: when TG_AUTH_BOT_TOKEN is set we long-poll both bots concurrently.
Login + link flows happen on whichever bot the user messages — replies go back
through the same bot, so deep-links pointing at the auth bot keep landing in the
auth bot's chat. Alerts continue to fan out from TG_BOT_TOKEN regardless.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from settings import settings

logger = logging.getLogger("avalant.tg")

# Per-bot poller state — keyed by token so adding a third bot is a one-line change.
_tasks: dict[str, asyncio.Task] = {}
_offsets: dict[str, int] = {}


async def _tg_post(token: str, method: str, payload: dict) -> dict | None:
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=35) as c:
            r = await c.post(url, json=payload)
            j = r.json()
            if not j.get("ok"):
                logger.debug("TG %s not ok: %s", method, j)
                return None
            return j
    except Exception as exc:
        logger.debug("TG %s error: %s", method, exc)
        return None


async def _handle_update(bot_token: str, bot_label: str, upd: dict) -> None:
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/start"):
        return

    chat = msg.get("chat") or {}
    frm  = msg.get("from") or {}
    chat_id = chat.get("id")
    tg_id = frm.get("id")
    username = (frm.get("username") or chat.get("username") or "").strip()
    first = (frm.get("first_name") or "").strip() or "there"

    if not chat_id or not tg_id:
        return

    # Extract /start payload: "/start link-<token>" or "/start <other>"
    parts = text.split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""

    from backend.db.base import SessionLocal
    from backend.db.models import User

    db = SessionLocal()
    reply_markup: dict | None = None
    try:
        # ── Login-by-bot flow (no auth required) ──
        if payload.startswith("auth-"):
            token = payload[len("auth-"):]
            from backend.services.tg_auth_service import consume_login_token
            uname = username.lstrip("@").lower() or None
            outcome = consume_login_token(db, token, int(tg_id), int(chat_id), uname, first)
            if outcome is None:
                reply = "🔒 This login link has expired or was already used. Generate a new one from the login page."
            elif outcome.startswith("Your account is blocked"):
                reply = "⛔ Your account is blocked. Contact support."
            else:
                # consume_login_token already wrote status=ok with the JWT.
                # Hand the user a clickable button that opens a fresh tab on
                # the website. Mobile browsers freeze the originating /login
                # tab when Telegram launches, killing the 2s poll for minutes
                # — the button-driven redirect bypasses that entirely.
                base = (settings.PUBLIC_BASE_URL or "https://avalant.xyz").rstrip("/")
                redeem_url = f"{base}/tg-done?t={token}"
                reply = (
                    f"✅ <b>Logged in</b>\n\n"
                    f"Tap the button below to open Avalant. "
                    f"The link is single-use and expires in 5 minutes."
                )
                reply_markup = {
                    "inline_keyboard": [[
                        {"text": "🔓 Open Avalant", "url": redeem_url}
                    ]]
                }
        # ── Preferred flow: deep-link token from profile ──
        elif payload.startswith("link-"):
            token = payload[len("link-"):]
            from backend.services.tg_auth_service import consume_link_token
            uname = username.lstrip("@").lower() or None
            user = consume_link_token(db, token, int(tg_id), int(chat_id), uname)
            if user:
                reply = (
                    f"✅ <b>Linked!</b>\n\n"
                    f"Avalant account <code>{user.username}</code> is now connected to this chat. "
                    f"You'll receive arbitrage alerts here when your thresholds trigger."
                )
            else:
                reply = (
                    "🔒 This link has expired, been used already, or is invalid.\n\n"
                    "Open <b>Profile → API Keys / Notifications</b> on Avalant and generate a fresh link."
                )
        # ── Fallback: legacy @username match ──
        elif username:
            uname = username.lstrip("@").lower()
            rows = (
                db.query(User)
                .filter(User.tg_username != None)  # noqa: E711
                .all()
            )
            match = next((u for u in rows if (u.tg_username or "").lstrip("@").lower() == uname), None)
            if match:
                match.tg_chat_id = int(chat_id)
                if not match.tg_id:
                    # Claim tg_id if no collision
                    if not db.query(User).filter(User.tg_id == int(tg_id), User.id != match.id).first():
                        match.tg_id = int(tg_id)
                db.commit()
                reply = (
                    f"✅ <b>Linked!</b>\n\n"
                    f"Avalant account <code>{match.username}</code> is now connected to this chat."
                )
                logger.info("TG chat linked via username (%s): user_id=%s chat_id=%s @%s",
                            bot_label, match.id, chat_id, uname)
            else:
                reply = (
                    f"Hi {first}! 👋\n\n"
                    f"This Telegram account isn't linked to Avalant yet.\n\n"
                    f"➡️ Go to <b>avalant.io → Profile</b> and tap "
                    f"<b>Link Telegram</b>. Then press Start here again — done."
                )
        else:
            reply = (
                f"Hi {first}! 👋\n\n"
                f"Open <b>Profile</b> on Avalant and tap <b>Link Telegram</b> to generate a one-time link."
            )
    except Exception as exc:
        logger.warning("TG handle /start error (%s): %s", bot_label, exc)
        reply = "Something went wrong. Try again in a moment."
    finally:
        db.close()

    msg_payload: dict = {
        "chat_id": chat_id,
        "text": reply,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        msg_payload["reply_markup"] = reply_markup
    await _tg_post(bot_token, "sendMessage", msg_payload)


async def _poll_loop(bot_token: str, bot_label: str) -> None:
    logger.info("TG bot polling started (%s)", bot_label)

    # On startup, drain any pending updates to avoid replaying old ones
    init = await _tg_post(bot_token, "getUpdates", {"offset": -1, "timeout": 0})
    if init and init.get("result"):
        _offsets[bot_token] = init["result"][-1]["update_id"] + 1
    else:
        _offsets[bot_token] = 0

    while True:
        try:
            j = await _tg_post(bot_token, "getUpdates", {
                "offset": _offsets.get(bot_token, 0),
                "timeout": 25,
            })
            if not j:
                await asyncio.sleep(5)
                continue
            for upd in j.get("result", []):
                _offsets[bot_token] = max(_offsets.get(bot_token, 0), upd["update_id"] + 1)
                try:
                    await _handle_update(bot_token, bot_label, upd)
                except Exception as exc:
                    logger.warning("TG update handler error (%s): %s", bot_label, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("TG poll loop error (%s): %s", bot_label, exc)
            await asyncio.sleep(5)


def start_tg_bot() -> None:
    """Start one poller per configured bot. Auth bot (if set) and notification
    bot are independent tasks; either can be missing without breaking the other."""
    bots: list[tuple[str, str]] = []
    if settings.TG_AUTH_BOT_TOKEN and settings.TG_AUTH_BOT_TOKEN != settings.TG_BOT_TOKEN:
        bots.append((settings.TG_AUTH_BOT_TOKEN, "auth"))
    if settings.TG_BOT_TOKEN:
        bots.append((settings.TG_BOT_TOKEN, "alerts"))

    if not bots:
        logger.info("TG bot tokens not set — bot polling disabled")
        return

    for token, label in bots:
        existing = _tasks.get(token)
        if existing and not existing.done():
            continue
        _tasks[token] = asyncio.create_task(_poll_loop(token, label))


def stop_tg_bot() -> None:
    for token, task in list(_tasks.items()):
        task.cancel()
    _tasks.clear()
    _offsets.clear()
