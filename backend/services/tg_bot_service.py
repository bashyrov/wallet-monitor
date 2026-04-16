"""Telegram bot long-polling — links user chats to Avalant accounts.

Users can't receive bot messages by @username alone — Telegram requires a numeric
chat_id, obtainable only after the user initiates the dialog with the bot. This
service long-polls getUpdates; on /start it looks up the Avalant user by the
sender's @username and stores the chat_id on that user row.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from settings import settings

logger = logging.getLogger("avalant.tg")

_task: asyncio.Task | None = None
_offset: int = 0


async def _tg_post(method: str, payload: dict) -> dict | None:
    if not settings.TG_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{settings.TG_BOT_TOKEN}/{method}"
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


async def _handle_update(upd: dict) -> None:
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/start"):
        return

    chat = msg.get("chat") or {}
    frm  = msg.get("from") or {}
    chat_id = chat.get("id")
    username = (frm.get("username") or chat.get("username") or "").strip()
    first = (frm.get("first_name") or "").strip() or "there"

    if not chat_id:
        return

    # Find Avalant user by tg_username (case-insensitive)
    from backend.db.base import SessionLocal
    from backend.db.models import User

    db = SessionLocal()
    try:
        if username:
            # normalise: strip possible leading @
            uname = username.lstrip("@").lower()
            user = (
                db.query(User)
                .filter(User.tg_username != None)  # noqa: E711
                .all()
            )
            match = next((u for u in user if (u.tg_username or "").lstrip("@").lower() == uname), None)
        else:
            match = None

        if match:
            match.tg_chat_id = chat_id
            db.commit()
            reply = (
                f"✅ <b>Linked!</b>\n\n"
                f"Avalant account <code>{match.username}</code> is now connected to this chat. "
                f"You'll receive arbitrage alerts here when your thresholds trigger."
            )
            logger.info("TG chat linked: user_id=%s chat_id=%s @%s", match.id, chat_id, uname)
        else:
            uname_display = f"@{uname}" if uname else "(no username)"
            reply = (
                f"Hi {first}! 👋\n\n"
                f"I don't see an Avalant account with Telegram {uname_display} yet.\n\n"
                f"➡️ Open your Avalant profile, set <b>Telegram username</b> to "
                f"<code>{uname}</code>, then send /start again.\n\n"
                f"(Make sure you have a public @username in Telegram Settings → Username.)"
            )
    except Exception as exc:
        logger.warning("TG handle /start error: %s", exc)
        reply = "Something went wrong. Try again in a moment."
    finally:
        db.close()

    await _tg_post("sendMessage", {
        "chat_id": chat_id,
        "text": reply,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


async def _poll_loop() -> None:
    global _offset
    logger.info("TG bot polling started")

    # On startup, drain any pending updates to avoid replaying old ones
    init = await _tg_post("getUpdates", {"offset": -1, "timeout": 0})
    if init and init.get("result"):
        _offset = init["result"][-1]["update_id"] + 1

    while True:
        try:
            j = await _tg_post("getUpdates", {"offset": _offset, "timeout": 25})
            if not j:
                await asyncio.sleep(5)
                continue
            for upd in j.get("result", []):
                _offset = max(_offset, upd["update_id"] + 1)
                try:
                    await _handle_update(upd)
                except Exception as exc:
                    logger.warning("TG update handler error: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("TG poll loop error: %s", exc)
            await asyncio.sleep(5)


def start_tg_bot() -> None:
    global _task
    if not settings.TG_BOT_TOKEN:
        logger.info("TG bot token not set — bot polling disabled")
        return
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_poll_loop())


def stop_tg_bot() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None
