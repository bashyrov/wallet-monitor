"""Promo-code lifecycle: validation, admin CRUD, stats.

Validation runs server-side at /checkout — the frontend may pre-validate
via /api/promo/validate, but the canonical authority is `validate_for_plan`
inside payment_service before creating the invoice. This keeps a malicious
client from sending an old or expired code as part of the checkout body.

Stacking is disabled — exactly one code per payment.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.db.models import Plan, PromoCode, PromoCodeUsage

logger = logging.getLogger("avalant.promo_service")


def _normalize(code: str) -> str:
    return (code or "").strip().upper()


def get_by_code(db: Session, code: str) -> PromoCode | None:
    return db.query(PromoCode).filter(PromoCode.code == _normalize(code)).first()


def is_eligible(promo: PromoCode | None, plan_id: int) -> bool:
    if not promo:
        return False
    if not promo.is_active:
        return False
    if promo.expires_at and promo.expires_at <= datetime.utcnow():
        return False
    if promo.max_uses is not None and (promo.used_count or 0) >= promo.max_uses:
        return False
    applies = promo.applies_to_plan_ids
    if applies:  # explicit list — must contain this plan
        try:
            allowed = {int(x) for x in applies}
        except Exception:
            allowed = set()
        if plan_id not in allowed:
            return False
    return True


def validate_for_plan(db: Session, code: str, plan_id: int) -> PromoCode | None:
    """Public surface for /api/promo/validate and the checkout flow.
    Returns the PromoCode row when usable, else None."""
    promo = get_by_code(db, code)
    if not is_eligible(promo, plan_id):
        return None
    return promo


# ── Admin CRUD ────────────────────────────────────────────────────────────────
_EDITABLE_FIELDS = {
    "discount_pct", "bonus_days", "max_uses", "applies_to_plan_ids",
    "is_active", "expires_at",
}


def list_codes(db: Session, *, only_active: bool = False) -> list[PromoCode]:
    q = db.query(PromoCode)
    if only_active:
        q = q.filter(PromoCode.is_active.is_(True))
    return q.order_by(PromoCode.created_at.desc()).all()


def _coerce_bonus_days(raw: Any) -> int:
    try:
        n = int(raw or 0)
    except (TypeError, ValueError):
        raise ValueError("bonus_days must be an integer")
    if n < 0:
        raise ValueError("bonus_days must be >= 0")
    if n > 3650:  # 10y guard against fat-finger
        raise ValueError("bonus_days too large (max 3650)")
    return n


def create_code(db: Session, code: str, fields: dict[str, Any]) -> PromoCode:
    code_n = _normalize(code)
    if not code_n:
        raise ValueError("code is required")
    if get_by_code(db, code_n):
        raise ValueError("code already exists")
    discount = Decimal(str(fields.get("discount_pct") or 0)).quantize(Decimal("0.01"))
    if discount < 0 or discount > 100:
        raise ValueError("discount_pct must be in [0, 100]")
    bonus_days = _coerce_bonus_days(fields.get("bonus_days"))
    if discount == 0 and bonus_days == 0:
        raise ValueError("promo must grant either a discount_pct > 0 or bonus_days > 0 (or both)")
    promo = PromoCode(
        code=code_n,
        discount_pct=discount,
        bonus_days=bonus_days,
        max_uses=fields.get("max_uses"),
        applies_to_plan_ids=fields.get("applies_to_plan_ids") or None,
        is_active=bool(fields.get("is_active", True)),
        expires_at=fields.get("expires_at"),
    )
    db.add(promo)
    db.commit()
    db.refresh(promo)
    return promo


def update_code(db: Session, promo: PromoCode, fields: dict[str, Any]) -> PromoCode:
    if "bonus_days" in fields:
        fields["bonus_days"] = _coerce_bonus_days(fields["bonus_days"])
    if "discount_pct" in fields and fields["discount_pct"] is not None:
        d = Decimal(str(fields["discount_pct"])).quantize(Decimal("0.01"))
        if d < 0 or d > 100:
            raise ValueError("discount_pct must be in [0, 100]")
        fields["discount_pct"] = d
    # After applying potential changes we need to ensure at least one of
    # discount_pct / bonus_days remains > 0 — same invariant as create.
    next_discount = fields.get("discount_pct", promo.discount_pct)
    next_bonus    = fields.get("bonus_days",   promo.bonus_days or 0)
    if Decimal(str(next_discount or 0)) == 0 and int(next_bonus or 0) == 0:
        raise ValueError("promo must grant either a discount_pct > 0 or bonus_days > 0 (or both)")
    for k, v in fields.items():
        if k in _EDITABLE_FIELDS:
            setattr(promo, k, v)
    db.commit()
    db.refresh(promo)
    return promo


def delete_code(db: Session, promo: PromoCode) -> None:
    """Hard-delete the code; usage rows survive via CASCADE rules. Stats
    queries that joined to promo_codes simply won't show this row anymore,
    but the historic revenue from `promo_code_usages` is queryable on its
    own (joined to `payments`)."""
    db.delete(promo)
    db.commit()


# ── Stats for admin dashboard ─────────────────────────────────────────────────
def stats(db: Session) -> list[dict[str, Any]]:
    """One row per code with use-count + revenue contributed."""
    rows = db.query(
        PromoCode.id,
        PromoCode.code,
        PromoCode.discount_pct,
        PromoCode.bonus_days,
        PromoCode.max_uses,
        PromoCode.used_count,
        PromoCode.is_active,
        PromoCode.expires_at,
        PromoCode.created_at,
    ).all()
    out = []
    for r in rows:
        # Sum final_amount_usd from related payments via promo_code_usages.
        from backend.db.models import Payment as _Pay
        revenue = (
            db.query(func.coalesce(func.sum(_Pay.final_amount_usd), 0))
            .join(PromoCodeUsage, PromoCodeUsage.payment_id == _Pay.id)
            .filter(PromoCodeUsage.promo_code_id == r.id, _Pay.status == "paid")
            .scalar()
        )
        out.append({
            "id": r.id,
            "code": r.code,
            "discount_pct": float(r.discount_pct),
            "bonus_days": int(r.bonus_days or 0),
            "max_uses": r.max_uses,
            "used_count": r.used_count or 0,
            "is_active": bool(r.is_active),
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "paid_revenue_usd": float(revenue or 0),
        })
    return out


def serialize_code(p: PromoCode) -> dict[str, Any]:
    return {
        "id": p.id,
        "code": p.code,
        "discount_pct": float(p.discount_pct),
        "bonus_days": int(p.bonus_days or 0),
        "max_uses": p.max_uses,
        "used_count": p.used_count or 0,
        "applies_to_plan_ids": p.applies_to_plan_ids or None,
        "is_active": bool(p.is_active),
        "expires_at": p.expires_at.isoformat() if p.expires_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }
