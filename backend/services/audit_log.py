"""Append-only audit log for admin + billing actions.

Single entrypoint: `record(db, request, current_user, action, target=..., delta=...)`.
Every destructive admin endpoint and every webhook-triggered state
change should call it. Failures are swallowed (logged) so an audit-log
write never blocks the original action — but every miss is a logged
warning so we can find gaps.

Querying: admins can list `/api/admin/audit-log` filtered by action /
target / actor / date.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from backend.db.models import AuditLogEntry, User

logger = logging.getLogger("avalant.audit_log")


def _ip(request: Request | None) -> str | None:
    if request is None:
        return None
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _ua(request: Request | None) -> str | None:
    if request is None:
        return None
    ua = request.headers.get("User-Agent")
    return ua[:300] if ua else None


def record(
    db: Session,
    *,
    request: Request | None,
    actor: User | None,
    action: str,
    target_type: str | None = None,
    target_id: int | None = None,
    delta: Any = None,
) -> None:
    try:
        entry = AuditLogEntry(
            actor_user_id=actor.id if actor else None,
            actor_ip=_ip(request),
            actor_user_agent=_ua(request),
            action=action,
            target_type=target_type,
            target_id=target_id,
            delta=delta,
        )
        db.add(entry)
        db.commit()
    except Exception as exc:
        logger.warning("audit_log write failed (action=%s): %s", action, exc)
        try:
            db.rollback()
        except Exception:
            pass


# Convenience: serialize for /api/admin/audit-log responses
def serialize(e: AuditLogEntry) -> dict[str, Any]:
    return {
        "id": e.id,
        "actor_user_id": e.actor_user_id,
        "actor_ip": e.actor_ip,
        "actor_user_agent": e.actor_user_agent,
        "action": e.action,
        "target_type": e.target_type,
        "target_id": e.target_id,
        "delta": e.delta,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }
