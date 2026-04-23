"""Background job that enforces `users.plan_expires_at`.

On the fetcher container a daemon thread wakes every 10 minutes, finds
users whose paid plan has expired, and downgrades them to `basic`. Admins
and users without an expiry set are skipped (their plan is managed
manually or is permanent).

No external cron needed; the loop lives in-process and starts from
fetcher/__main__.py. Idempotent — running twice is harmless.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from backend.db.base import SessionLocal
from backend.db.models import User

logger = logging.getLogger("avalant.plan_expiry")

_CHECK_INTERVAL_S = 600.0   # 10 minutes
_thread: threading.Thread | None = None
_stop_evt: threading.Event | None = None


def _run_once() -> int:
    """Return number of users downgraded in this pass."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        rows = (
            db.query(User)
            .filter(
                User.plan_expires_at.isnot(None),
                User.plan_expires_at < now,
                User.plan != "basic",
                User.is_admin == False,  # noqa: E712
            )
            .all()
        )
        count = 0
        for u in rows:
            prior_plan = u.plan
            u.plan = "basic"
            u.plan_expires_at = None
            db.add(u)
            logger.info(
                "plan expired: uid=%s %s → basic (expired at %s)",
                u.id, prior_plan, u.plan_expires_at,
            )
            count += 1
        if count:
            db.commit()
            # Purge the Redis auth cache for every downgraded user so the
            # next authenticated request sees the new plan.
            try:
                from backend.services.auth_cache import invalidate_user
                for u in rows:
                    invalidate_user(u.id)
            except Exception:
                pass
        return count
    finally:
        db.close()


def _loop(stop_evt: threading.Event) -> None:
    logger.info("plan expiry loop started (interval=%.0fs)", _CHECK_INTERVAL_S)
    while not stop_evt.is_set():
        try:
            n = _run_once()
            if n:
                logger.info("plan expiry pass: downgraded %d user(s)", n)
        except Exception as exc:
            logger.warning("plan expiry pass failed: %s", exc)
        stop_evt.wait(_CHECK_INTERVAL_S)


def start_plan_expiry_service() -> None:
    global _thread, _stop_evt
    if _thread is not None and _thread.is_alive():
        return
    _stop_evt = threading.Event()
    _thread = threading.Thread(target=_loop, args=(_stop_evt,),
                               name="plan-expiry", daemon=True)
    _thread.start()


def stop_plan_expiry_service() -> None:
    global _thread, _stop_evt
    if _stop_evt is not None:
        _stop_evt.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=3.0)
    _thread = None
    _stop_evt = None
