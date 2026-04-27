"""Telegram bot long-polling — links user chats to Avalant accounts.

Users can't receive bot messages by @username alone — Telegram requires a numeric
chat_id, obtainable only after the user initiates the dialog with the bot. This
service long-polls getUpdates; on /start it looks up the Avalant user by the
sender's @username and stores the chat_id on that user row.

Two-bot mode: when TG_AUTH_BOT_TOKEN is set we long-poll both bots concurrently.
Login + link flows happen on whichever bot the user messages — replies go back
through the same bot, so deep-links pointing at the auth bot keep landing in the
auth bot's chat. Alerts continue to fan out from TG_BOT_TOKEN regardless.

Multi-replica safety: Telegram returns each update to exactly one long-poll
client, so two replicas calling getUpdates would race — half the updates would
land on each, and the other replica would silently sit on its 25 s timeout.
Solution: every poll loop tries to acquire a Redis lock keyed by bot_token
hash (TTL 30 s, renewed every 10 s). Only the leader polls; followers sleep
and retry. If the leader crashes the lock expires and the next replica picks
it up within ~30 s. Without Redis we fall back to single-replica polling
(every replica polls — same race as before, but at least nothing is dropped
on a single-instance dev box).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
import uuid

import httpx

from settings import settings

logger = logging.getLogger("avalant.tg")

# Per-bot poller state — keyed by token so adding a third bot is a one-line change.
_tasks: dict[str, asyncio.Task] = {}
_offsets: dict[str, int] = {}

# Stable id for this process — printed in lock-acquired logs so multi-replica
# leader transitions are debuggable.
_INSTANCE_ID = uuid.uuid4().hex[:12]

# ── Redis-backed leader election ─────────────────────────────────────────────
# Lease + renew tuned so a slow getUpdates (TG occasionally takes 25–35 s)
# OR a slow _handle_update (PgBouncer pool pressure under arb-fetch storms)
# can't expire the lease mid-loop. The renewer runs in its own task at
# fixed cadence — the polling loop never has to remember to renew.
_LOCK_TTL_S = 60
_LOCK_RENEW_S = 12
_LOCK_RETRY_S = 3
_GETUPDATES_TIMEOUT_S = 15
_redis_client = None


def _get_redis():
    """Lazy + cached. Single-replica dev with no REDIS_URL → returns None and
    polling proceeds without leader election (the only replica IS the leader)."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    url = os.environ.get("REDIS_URL") or ""
    if not url:
        return None
    try:
        import redis
        _redis_client = redis.from_url(
            url,
            socket_connect_timeout=1.0,
            socket_timeout=1.5,
            health_check_interval=30,
        )
        _redis_client.ping()
        return _redis_client
    except Exception as exc:
        logger.warning("tg_bot_service: redis unavailable (%s) — leader election disabled", exc)
        _redis_client = None
        return None


def _lock_key(bot_token: str) -> str:
    """Stable 16-hex digest of the bot token. Token never written in cleartext."""
    return "tg_bot_lock:" + hashlib.sha256(bot_token.encode()).hexdigest()[:16]


# Persistent httpx client per (timeout, label). Reusing the connection
# pool saves the ~200 ms TLS handshake on every call AND keeps a healthy
# HTTP/2 connection alive — which is what Telegram's edge prefers under
# their RPS limits. Two clients: one with a long read timeout for
# getUpdates (TG holds it ≤25 s by design) and one short for everything
# else (sends, callbacks, etc.).
_tg_client_long: httpx.AsyncClient | None = None
_tg_client_short: httpx.AsyncClient | None = None


def _tg_client(method: str) -> httpx.AsyncClient:
    global _tg_client_long, _tg_client_short
    is_long = method.startswith("getUpdates")
    if is_long:
        if _tg_client_long is None or _tg_client_long.is_closed:
            _tg_client_long = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=35.0, write=5.0, pool=5.0),
                http2=True,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=120),
            )
        return _tg_client_long
    if _tg_client_short is None or _tg_client_short.is_closed:
        _tg_client_short = httpx.AsyncClient(
            # sendMessage normally returns in <500 ms. Tight timeouts:
            # if Contabo's path to api.telegram.org degrades we fail fast
            # and don't block the poll loop for 35 s.
            timeout=httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=3.0),
            http2=True,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8, keepalive_expiry=120),
        )
    return _tg_client_short


async def _tg_post(token: str, method: str, payload: dict) -> dict | None:
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = await _tg_client(method).post(url, json=payload)
        j = r.json()
        if not j.get("ok"):
            logger.warning("TG %s not ok: %s", method, j)
            return None
        return j
    except Exception as exc:
        logger.warning("TG %s error: %s", method, exc)
        return None


async def _handle_update(bot_token: str, bot_label: str, upd: dict) -> None:
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/start"):
        return
    _t0 = time.time()
    # Server-side message timestamp from TG (seconds). If our handler runs
    # more than ~2 s after the user actually pressed Start, we want to know
    # — that's the bug class the user reported.
    _msg_ts = msg.get("date") or 0
    _wire_lag = (_t0 - _msg_ts) if _msg_ts else None

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
        # Telegram usernames can change at any time; chat_id + tg_id stay stable.
        # Refresh tg_username for any user we already know by tg_id so an
        # @-handle swap doesn't break the legacy username-fallback path.
        if tg_id:
            try:
                u = db.query(User).filter(User.tg_id == int(tg_id)).first()
                if u is not None:
                    new_uname = (username.lstrip("@").lower() or None)
                    if new_uname and (u.tg_username or "").lstrip("@").lower() != new_uname:
                        u.tg_username = new_uname
                    if u.tg_chat_id != int(chat_id):
                        u.tg_chat_id = int(chat_id)
                    db.commit()
            except Exception:
                db.rollback()

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

    # Fire-and-forget the reply send. Critical: when Contabo's network path
    # to api.telegram.org degrades, sendMessage can take 35 s. Awaiting it
    # blocks the poll loop — the next /start from anyone (or the next
    # leader-renew check on this bot) would have to wait. By detaching the
    # send into its own task we keep getUpdates flowing immediately. The
    # JWT for the login flow is ALREADY persisted (consume_login_token wrote
    # the file before this point), so the user's webview poll on /tg-done
    # picks it up regardless of how slow the bot's button reply lands.
    _t_send = time.time()
    async def _send_and_log():
        try:
            await _tg_post(bot_token, "sendMessage", msg_payload)
            logger.info(
                "TG /start handled (%s): handler=%.2fs send=%.2fs wire_lag=%s payload=%r",
                bot_label, _t_send - _t0, time.time() - _t_send,
                f"{_wire_lag:.2f}s" if _wire_lag is not None else "?",
                payload[:30] if payload else "<no-payload>",
            )
        except Exception as exc:
            logger.warning("TG sendMessage failed (%s): %s", bot_label, exc)
    asyncio.create_task(_send_and_log())


async def _drain_offset(bot_token: str) -> None:
    """One-time drain of pending updates so we don't replay old ones on a
    fresh leader takeover. Called once when this replica becomes leader."""
    init = await _tg_post(bot_token, "getUpdates", {"offset": -1, "timeout": 0})
    if init and init.get("result"):
        _offsets[bot_token] = init["result"][-1]["update_id"] + 1
    elif bot_token not in _offsets:
        _offsets[bot_token] = 0


def _try_acquire(redis_client, key: str) -> bool:
    """SET NX EX — atomic lock acquire."""
    try:
        return bool(redis_client.set(key, _INSTANCE_ID, nx=True, ex=_LOCK_TTL_S))
    except Exception as exc:
        logger.warning("tg_bot_service: redis SET NX failed (%s)", exc)
        return False


def _renew_lock(redis_client, key: str) -> bool:
    """Extend TTL only if we still hold the lock. Compare-and-set via Lua so
    we never accidentally renew a key a different replica took over."""
    lua = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('pexpire', KEYS[1], ARGV[2])
    else
      return 0
    end
    """
    try:
        return bool(redis_client.eval(lua, 1, key, _INSTANCE_ID, _LOCK_TTL_S * 1000))
    except Exception as exc:
        logger.warning("tg_bot_service: redis renew failed (%s)", exc)
        return False


def _release_lock(redis_client, key: str) -> None:
    """Compare-and-delete — only release if we still own it."""
    lua = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('del', KEYS[1])
    else
      return 0
    end
    """
    try:
        redis_client.eval(lua, 1, key, _INSTANCE_ID)
    except Exception:
        pass


def _renew_thread(bot_label: str, redis_client, lock_key: str, lost: threading.Event) -> None:
    """Renewer running on its own OS thread. The fetcher's asyncio loop is
    shared by 11+ WS adapters, screener compute, prewarm, alpha jobs etc. —
    any one of those occasionally blocks the loop for 30–60 s with a sync
    call. An asyncio-based renewer would silently miss its tick during
    those windows and the lock TTL would lapse. A real thread runs
    independently of the loop and is immune to that class of stall.

    Communication back to the asyncio poll loop uses threading.Event,
    which the poll loop polls non-blockingly (asyncio.Event isn't
    thread-safe without call_soon_threadsafe and that's needless ceremony
    for a one-shot signal)."""
    while not lost.wait(timeout=_LOCK_RENEW_S):
        if not _renew_lock(redis_client, lock_key):
            logger.warning("TG bot %s renewer: lost leader lock", bot_label)
            lost.set()
            return


async def _lead_and_poll(bot_token: str, bot_label: str, redis_client, lock_key: str) -> None:
    """Long-poll loop. Renewal runs in a SIBLING THREAD (not asyncio task)
    so a stalled event loop can't make us miss the renew tick. Exits when
    renewal fails or asyncio cancels us."""
    await _drain_offset(bot_token)
    lost = threading.Event()
    renewer: threading.Thread | None = None
    if redis_client is not None:
        renewer = threading.Thread(
            target=_renew_thread,
            args=(bot_label, redis_client, lock_key, lost),
            name=f"tg-renew-{bot_label}",
            daemon=True,
        )
        renewer.start()

    try:
        while not lost.is_set():
            try:
                # threading.Event isn't awaitable, so we can't asyncio.wait on it.
                # The compromise: cap getUpdates timeout to _GETUPDATES_TIMEOUT_S
                # and check `lost` between polls. Worst-case lag from
                # losing leadership to stepping down is one poll cycle
                # (≤ _GETUPDATES_TIMEOUT_S), still tighter than the previous
                # 25 s.
                j = await _tg_post(bot_token, "getUpdates", {
                    "offset": _offsets.get(bot_token, 0),
                    "timeout": _GETUPDATES_TIMEOUT_S,
                })
                if lost.is_set():
                    return
                if j:
                    for upd in j.get("result", []):
                        if lost.is_set():
                            return
                        _offsets[bot_token] = max(_offsets.get(bot_token, 0), upd["update_id"] + 1)
                        try:
                            await _handle_update(bot_token, bot_label, upd)
                        except Exception as exc:
                            logger.warning("TG update handler error (%s): %s", bot_label, exc)
                else:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("TG poll loop error (%s): %s", bot_label, exc)
                await asyncio.sleep(2)
    finally:
        # Tell the renewer to exit; the daemon thread dies with the process
        # if the join hangs.
        lost.set()
        if renewer is not None:
            renewer.join(timeout=2.0)


async def _poll_loop(bot_token: str, bot_label: str) -> None:
    """Outer loop. With Redis: try to become leader, lead, repeat on step-down.
    Without Redis: just lead forever (single-replica fallback)."""
    redis_client = _get_redis()
    lock_key = _lock_key(bot_token)
    if redis_client is None:
        logger.info("TG bot %s polling started (no leader election — Redis unavailable)", bot_label)
        await _lead_and_poll(bot_token, bot_label, None, lock_key)
        return

    while True:
        try:
            if _try_acquire(redis_client, lock_key):
                logger.info("TG bot %s leader acquired (instance=%s)", bot_label, _INSTANCE_ID)
                try:
                    await _lead_and_poll(bot_token, bot_label, redis_client, lock_key)
                finally:
                    _release_lock(redis_client, lock_key)
                # We just stepped down — don't busy-loop trying to reacquire.
                await asyncio.sleep(2)
            else:
                # Someone else holds the lock. Wait for it to expire, then retry.
                await asyncio.sleep(_LOCK_RETRY_S)
        except asyncio.CancelledError:
            _release_lock(redis_client, lock_key)
            raise
        except Exception as exc:
            logger.warning("TG bot %s outer loop error: %s", bot_label, exc)
            await asyncio.sleep(_LOCK_RETRY_S)


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
