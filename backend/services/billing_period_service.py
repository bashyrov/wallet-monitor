"""Billing-period catalogue helpers.

Pricing is `base_monthly × months × (1 - discount_pct/100)`. This module
centralises lookups, serialisation, admin CRUD, and pricing computation.

`get_period(db, period_id)` and the admin CRUD never raise on a missing
row — callers handle the None.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy.orm import Session

from backend.db.models import BillingPeriod


def list_periods(db: Session, *, only_active: bool = True) -> list[BillingPeriod]:
    q = db.query(BillingPeriod)
    if only_active:
        q = q.filter(BillingPeriod.is_active.is_(True))
    return q.order_by(BillingPeriod.sort_order.asc(), BillingPeriod.id.asc()).all()


def get_period(db: Session, period_id: int) -> BillingPeriod | None:
    return db.query(BillingPeriod).filter(BillingPeriod.id == period_id).first()


def get_period_by_slug(db: Session, slug: str) -> BillingPeriod | None:
    return db.query(BillingPeriod).filter(BillingPeriod.slug == slug).first()


def serialize(period: BillingPeriod) -> dict[str, Any]:
    return {
        "id": period.id,
        "slug": period.slug,
        "label": period.label,
        "months": period.months,
        "discount_pct": float(period.discount_pct or 0),
        "sort_order": period.sort_order,
        "is_active": bool(period.is_active),
    }


def compute_total(base_monthly_usd: Decimal | float, period: BillingPeriod) -> Decimal:
    """Total amount the user pays for committing to `period.months`.
    Formula: base × months × (1 - discount/100), rounded to 2 decimals."""
    base = Decimal(str(base_monthly_usd or 0))
    months = Decimal(period.months or 1)
    discount = Decimal(period.discount_pct or 0)
    total = base * months * (Decimal("100") - discount) / Decimal("100")
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ── Admin CRUD ────────────────────────────────────────────────────────────────
_EDITABLE_FIELDS = {"label", "months", "discount_pct", "sort_order", "is_active"}


def create(db: Session, slug: str, fields: dict[str, Any]) -> BillingPeriod:
    if not slug or not slug.replace("_", "").replace("-", "").isalnum():
        raise ValueError("slug must be alnum/-_")
    if get_period_by_slug(db, slug):
        raise ValueError("slug already exists")
    months = int(fields.get("months") or 0)
    if months <= 0:
        raise ValueError("months must be > 0")
    period = BillingPeriod(
        slug=slug,
        label=fields.get("label") or slug.title(),
        months=months,
        discount_pct=Decimal(str(fields.get("discount_pct") or 0)),
        sort_order=int(fields.get("sort_order") or 0),
        is_active=bool(fields.get("is_active", True)),
    )
    db.add(period)
    db.commit()
    db.refresh(period)
    return period


def update(db: Session, period: BillingPeriod, fields: dict[str, Any]) -> BillingPeriod:
    for k, v in fields.items():
        if k in _EDITABLE_FIELDS:
            setattr(period, k, v)
    db.commit()
    db.refresh(period)
    return period


def delete(db: Session, period: BillingPeriod) -> None:
    """Soft-delete: set inactive instead of removing rows so historic
    Payment FK rows stay valid."""
    period.is_active = False
    db.commit()
