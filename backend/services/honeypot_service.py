"""Honeypot / IDS — auto-ban users who probe admin surfaces.

Threat model:
- A logged-in non-admin user shouldn't see /admin or any /api/admin/*
  endpoint. Browser navigation already returns a 302/403, so legitimate
  flows can't accidentally trigger this trap.
- A determined attacker DOES poke admin URLs to enumerate (the auth
  middleware tells them what's there via the redirect target).

Response:
- First admin-probe by a logged-in non-admin → block account immediately.
- Future requests from a blocked account already fail at get_current_user
  (HTTP 403). User sees "your account is blocked" and is asked to contact
  support.
- Anonymous probes (no Bearer token) get the standard 401 response and
  are NOT auto-banned — that path is too noisy for crawlers / dependency
  updates / port scans.

Logging:
- Every probe is logged at WARNING with IP, user_id, path, method.
- audit_log gets a row tagged "security.admin_probe_block" so the
  incident appears in /admin → Audit log.
- admin_alert_service fires a TG message to admins so they can review.

This module sits behind the admin/get_admin_user dependency — calling
trip(...) is a one-liner from any code path that detects a violation.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.models import User

logger = logging.getLogger("avalant.honeypot")


def trip(
    db: Session,
    user: User | None,
    *,
    request_ip: str | None = None,
    request_path: str | None = None,
    request_method: str | None = None,
    reason: str = "admin_probe",
) -> None:
    """Auto-block the user, write an audit row, alert admins. Idempotent —
    re-tripping an already-blocked user does nothing extra."""
    if user is None:
        return
    if getattr(user, "is_blocked", False):
        return
    try:
        user.is_blocked = True
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("honeypot trip db.commit failed: %s", exc)
        return

    logger.warning(
        "HONEYPOT: blocked user_id=%s ip=%s method=%s path=%s reason=%s",
        user.id, request_ip, request_method, request_path, reason,
    )

    try:
        from backend.services.auth_cache import invalidate_user
        invalidate_user(user.id)
    except Exception:
        pass

    try:
        from backend.services import audit_log
        audit_log.record_low_level(
            db,
            actor_user_id=user.id,
            actor_ip=request_ip,
            action="security.admin_probe_block",
            target_type="user",
            target_id=user.id,
            delta={"path": request_path, "method": request_method, "reason": reason},
        )
    except Exception as exc:
        logger.debug("honeypot audit_log write failed: %s", exc)

    try:
        from backend.services.admin_alert_service import notify_admins
        notify_admins(
            f"⚠️ <b>Admin probe blocked</b>\n"
            f"User: <code>{user.username}</code> (#{user.id})\n"
            f"IP: <code>{request_ip or '?'}</code>\n"
            f"Path: <code>{request_method or '?'} {request_path or '?'}</code>\n"
            f"Reason: {reason}\n\n"
            f"User has been auto-blocked. Unblock from <b>/admin → Users</b> if false-positive.",
        )
    except Exception:
        pass
