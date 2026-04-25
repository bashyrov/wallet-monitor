"""Promotional popups — admin-managed, per-user dismiss state.

Targeting:
  · target_type='everyone'      — both authenticated AND anonymous visitors
  · target_type='authenticated' — every logged-in user (legacy 'all' maps here)
  · target_type='anonymous'     — only logged-out visitors
  · target_type='user'          — only the named target_user_id sees it

Frequency:
  · 'once'              — once dismissed, never re-shows.
  · 'every_n_min'       — re-eligible after `frequency_minutes` since the
                          last dismiss.

Anonymous users have no DB row to track dismissals — the loader caches
dismissed ids in localStorage instead. So the server returns every
eligible 'anonymous'/'everyone' popup; the client filters out ones the
visitor already closed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from backend.db.models import Popup, PopupDismissal, User

logger = logging.getLogger("avalant.popup_service")


def _is_dismissed(
    db: Session, popup: Popup, user_id: int, *, now: datetime | None = None,
) -> bool:
    now = now or datetime.utcnow()
    dis = (
        db.query(PopupDismissal)
        .filter(
            PopupDismissal.popup_id == popup.id,
            PopupDismissal.user_id == user_id,
        )
        .first()
    )
    if not dis:
        return False
    if popup.frequency_type == "every_n_min" and popup.frequency_minutes > 0:
        re_eligible_at = dis.dismissed_at + timedelta(minutes=popup.frequency_minutes)
        return now < re_eligible_at
    # "once" or any unknown — once dismissed always dismissed.
    return True


def get_pending_for_user(db: Session, user: User) -> list[dict[str, Any]]:
    """Return active popups eligible to show to this logged-in user right now.
    Includes legacy 'all' rows in case the migration hasn't run yet."""
    now = datetime.utcnow()
    q = (
        db.query(Popup)
        .filter(Popup.is_active.is_(True))
        .filter(
            Popup.target_type.in_(("everyone", "authenticated", "all"))
            | ((Popup.target_type == "user") & (Popup.target_user_id == user.id))
        )
        .order_by(Popup.created_at.asc())
    )
    out = []
    for popup in q.all():
        if _is_dismissed(db, popup, user.id, now=now):
            continue
        out.append(serialize_popup(popup))
    return out


def get_pending_for_anonymous(db: Session) -> list[dict[str, Any]]:
    """Return active popups eligible to show to a logged-out visitor.
    Caller (the JS loader) is responsible for filtering out ones the visitor
    has already dismissed via localStorage — the server has no dismiss row
    to consult since there's no user_id."""
    q = (
        db.query(Popup)
        .filter(Popup.is_active.is_(True))
        .filter(Popup.target_type.in_(("everyone", "anonymous")))
        .order_by(Popup.created_at.asc())
    )
    return [serialize_popup(p) for p in q.all()]


def dismiss(db: Session, popup_id: int, user_id: int) -> bool:
    popup = db.query(Popup).filter(Popup.id == popup_id).first()
    if not popup:
        return False
    now = datetime.utcnow()
    dis = (
        db.query(PopupDismissal)
        .filter(
            PopupDismissal.popup_id == popup.id,
            PopupDismissal.user_id == user_id,
        )
        .first()
    )
    if dis:
        dis.dismissed_at = now
    else:
        db.add(PopupDismissal(popup_id=popup.id, user_id=user_id, dismissed_at=now))
    db.commit()
    return True


# ── Admin CRUD ────────────────────────────────────────────────────────────────
_EDITABLE_FIELDS = {
    "title", "body", "button_text", "button_url",
    "target_type", "target_user_id",
    "frequency_type", "frequency_minutes",
    "is_active",
}


def list_popups(db: Session, *, only_active: bool = False) -> list[Popup]:
    q = db.query(Popup)
    if only_active:
        q = q.filter(Popup.is_active.is_(True))
    return q.order_by(Popup.created_at.desc()).all()


def create_popup(db: Session, fields: dict[str, Any]) -> Popup:
    title = (fields.get("title") or "").strip()
    body = (fields.get("body") or "").strip()
    if not title or not body:
        raise ValueError("title and body are required")
    target_type = fields.get("target_type") or "authenticated"
    if target_type == "all":
        target_type = "authenticated"  # legacy alias
    if target_type not in ("everyone", "authenticated", "anonymous", "user"):
        raise ValueError(
            "target_type must be one of 'everyone' | 'authenticated' | 'anonymous' | 'user'"
        )
    if target_type == "user" and not fields.get("target_user_id"):
        raise ValueError("target_user_id is required when target_type='user'")
    if target_type != "user":
        fields["target_user_id"] = None  # clear stale id when targeting broadly
    frequency_type = fields.get("frequency_type") or "once"
    if frequency_type not in ("once", "every_n_min"):
        raise ValueError("frequency_type must be 'once' or 'every_n_min'")
    frequency_minutes = int(fields.get("frequency_minutes") or 0)
    if frequency_type == "every_n_min" and frequency_minutes < 1:
        raise ValueError("frequency_minutes must be >= 1 for every_n_min")
    popup = Popup(
        title=title,
        body=body,
        button_text=(fields.get("button_text") or "View pricing"),
        button_url=(fields.get("button_url") or "/pricing"),
        target_type=target_type,
        target_user_id=fields.get("target_user_id"),
        frequency_type=frequency_type,
        frequency_minutes=frequency_minutes,
        is_active=bool(fields.get("is_active", True)),
    )
    db.add(popup)
    db.commit()
    db.refresh(popup)
    return popup


def update_popup(db: Session, popup: Popup, fields: dict[str, Any]) -> Popup:
    if fields.get("target_type") == "all":
        fields["target_type"] = "authenticated"  # legacy alias
    new_tt = fields.get("target_type", popup.target_type)
    if new_tt not in (None, "everyone", "authenticated", "anonymous", "user"):
        raise ValueError(
            "target_type must be one of 'everyone' | 'authenticated' | 'anonymous' | 'user'"
        )
    if new_tt and new_tt != "user":
        # Audience widened — drop the stale user pin so the row can't accidentally
        # behave like a user-targeted popup later.
        fields["target_user_id"] = None
    for k, v in fields.items():
        if k in _EDITABLE_FIELDS:
            setattr(popup, k, v)
    db.commit()
    db.refresh(popup)
    return popup


def delete_popup(db: Session, popup: Popup) -> None:
    """Hard-delete; popup_dismissals rows go via CASCADE."""
    db.delete(popup)
    db.commit()


def serialize_popup(p: Popup) -> dict[str, Any]:
    return {
        "id": p.id,
        "title": p.title,
        "body": p.body,
        "button_text": p.button_text,
        "button_url": p.button_url,
        "target_type": p.target_type,
        "target_user_id": p.target_user_id,
        "frequency_type": p.frequency_type,
        "frequency_minutes": p.frequency_minutes,
        "is_active": bool(p.is_active),
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }
