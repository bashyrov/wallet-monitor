"""Resolve a user's *effective* plan and the limits that flow from it.

Source of truth: `users.plan_id` → `plans` row. Once the plan is set in the
DB it stays put even after `plan_expires_at` passes — we just downgrade the
*effective* limits on read so the user keeps seeing their plan, but can no
longer add wallets above the free cap.

Limits returned:
  · portfolio_limit              — max wallets where purpose='portfolio'
                                   (after expiry: drops to portfolio_limit_grace)
  · exchange_keys_per_venue      — max wallets per (wallet_type, type_value)
                                   (after expiry: drops to free plan's value)
  · trade_delay_ms               — sleep this long before placing orders
                                   (after expiry: free plan's value)

Exposes:
  - get_plan(db, plan_id)            — fetch by id
  - get_plan_by_slug(db, slug)       — fetch by slug
  - get_free_plan(db)                — convenience: the seed Free plan
  - get_user_plan(db, user)          — full Plan row, never None (falls back
                                       to free if user.plan_id is null)
  - effective_limits(db, user)       — dict of resolved limits with grace
                                       semantics applied
  - is_paid(user, plan)              — True when user has an active paid plan
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from backend.db.models import Plan, User

logger = logging.getLogger("avalant.plan_service")


# Sentinel used in DB for "unlimited" — Plan.portfolio_limit = -1 means
# the user can keep adding wallets forever; `is_unlimited()` below makes
# the read-side intent explicit and adapts every consumer (wallet limit
# checks, frontend pills, /balance truncation).
UNLIM = -1


def is_unlimited(value: int | None) -> bool:
    return value is None or value < 0


@dataclass
class EffectiveLimits:
    plan_id: int
    plan_slug: str
    plan_name: str
    is_free: bool
    is_expired: bool
    has_portfolio: bool
    # portfolio_limit / exchange_keys_per_venue == -1 means "unlimited";
    # call sites should prefer the helper rather than treating the int
    # value directly.
    portfolio_limit: int
    exchange_keys_per_venue: int
    trade_delay_ms: int
    expires_at: datetime | None

    @property
    def portfolio_unlimited(self) -> bool:
        return is_unlimited(self.portfolio_limit)

    @property
    def keys_unlimited(self) -> bool:
        return is_unlimited(self.exchange_keys_per_venue)


# Plan rows change rarely (admin-only via set_plan / set_value). /me hits
# this on every authenticated request — typically 3 DB queries per call
# (get_user_plan + get_free_plan + wallet count). At 50 req/s on app + app2
# that's 300 DB queries/s just for plan resolution. Cache the rows in-process
# with a short TTL; admin mutations call invalidate_plan_cache() to flush.
_PLAN_CACHE_TTL_S = 60.0
_plan_cache: dict[str, tuple[Any, float]] = {}


def _cache_get(key: str) -> Any:
    entry = _plan_cache.get(key)
    if entry and (time.monotonic() - entry[1]) < _PLAN_CACHE_TTL_S:
        return entry[0]
    return None


def _cache_set(key: str, value: Any) -> None:
    _plan_cache[key] = (value, time.monotonic())


def invalidate_plan_cache() -> None:
    """Flush the per-process plan cache. Called from set_plan, plans CRUD,
    and set_value when KEY_HIDDEN_SYMBOLS / similar plan-affecting keys
    change."""
    _plan_cache.clear()


def get_plan(db: Session, plan_id: int) -> Plan | None:
    cache_key = f"id:{plan_id}"
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit
    plan = db.query(Plan).filter(Plan.id == plan_id).first()
    if plan is not None:
        _cache_set(cache_key, plan)
    return plan


def get_plan_by_slug(db: Session, slug: str) -> Plan | None:
    cache_key = f"slug:{slug}"
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit
    plan = db.query(Plan).filter(Plan.slug == slug).first()
    if plan is not None:
        _cache_set(cache_key, plan)
    return plan


def get_free_plan(db: Session) -> Plan | None:
    cache_key = "free"
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit
    plan = (
        db.query(Plan)
        .filter(Plan.is_free.is_(True), Plan.is_active.is_(True))
        .order_by(Plan.sort_order.asc(), Plan.id.asc())
        .first()
    )
    if plan is not None:
        _cache_set(cache_key, plan)
    return plan


def get_user_plan(db: Session, user: User) -> Plan:
    if user.plan_id:
        plan = get_plan(db, user.plan_id)
        if plan and plan.is_active:
            return plan
    free = get_free_plan(db)
    if not free:
        raise RuntimeError("No active free plan in DB — seed migration missing")
    return free


def is_paid_active(user: User, plan: Plan) -> bool:
    """Paid plan with no expiry yet (or expiry in the future)."""
    if plan.is_free:
        return False
    if user.plan_expires_at is None:
        return True
    return user.plan_expires_at > datetime.utcnow()


def effective_limits(db: Session, user: User) -> EffectiveLimits:
    plan = get_user_plan(db, user)
    free = get_free_plan(db) or plan
    is_expired = (
        not plan.is_free
        and user.plan_expires_at is not None
        and user.plan_expires_at <= datetime.utcnow()
    )
    if is_expired:
        portfolio_limit = plan.portfolio_limit_grace
        exchange_keys_per_venue = free.exchange_keys_per_venue
        trade_delay_ms = free.trade_delay_ms
    else:
        portfolio_limit = plan.portfolio_limit
        exchange_keys_per_venue = plan.exchange_keys_per_venue
        trade_delay_ms = plan.trade_delay_ms
    return EffectiveLimits(
        plan_id=plan.id,
        plan_slug=plan.slug,
        plan_name=plan.name,
        is_free=bool(plan.is_free),
        is_expired=is_expired,
        has_portfolio=bool(getattr(plan, "has_portfolio", True)),
        portfolio_limit=int(portfolio_limit),
        exchange_keys_per_venue=int(exchange_keys_per_venue),
        trade_delay_ms=int(trade_delay_ms),
        expires_at=user.plan_expires_at,
    )


def serialize_plan(plan: Plan) -> dict[str, Any]:
    """Public-facing JSON shape for /api/plans + admin CRUD responses."""
    return {
        "id": plan.id,
        "slug": plan.slug,
        "name": plan.name,
        "description": plan.description,
        "price_usd_monthly": float(plan.price_usd_monthly or 0),
        "price_usd_annual": float(plan.price_usd_annual or 0),
        "portfolio_limit": plan.portfolio_limit,
        "portfolio_limit_grace": plan.portfolio_limit_grace,
        "exchange_keys_per_venue": plan.exchange_keys_per_venue,
        "trade_delay_ms": plan.trade_delay_ms,
        "has_portfolio": bool(getattr(plan, "has_portfolio", True)),
        "is_subscription": bool(getattr(plan, "is_subscription", True)),
        "is_admin_only": bool(getattr(plan, "is_admin_only", False)),
        "features": plan.features or {"perks": [], "limits": []},
        "is_free": bool(plan.is_free),
        "is_active": bool(plan.is_active),
        "sort_order": plan.sort_order,
    }


def list_plans(db: Session, *, only_active: bool = True, public_only: bool = False) -> list[Plan]:
    q = db.query(Plan)
    if only_active:
        q = q.filter(Plan.is_active.is_(True))
    if public_only:
        q = q.filter(Plan.is_admin_only.is_(False))
    return q.order_by(Plan.sort_order.asc(), Plan.id.asc()).all()


# ── Admin CRUD ────────────────────────────────────────────────────────────────
_EDITABLE_FIELDS = {
    "name", "description",
    "price_usd_monthly", "price_usd_annual",
    "portfolio_limit", "portfolio_limit_grace",
    "exchange_keys_per_venue", "trade_delay_ms",
    "has_portfolio", "is_subscription",
    "features", "is_active", "sort_order",
}


def update_plan(db: Session, plan: Plan, fields: dict[str, Any]) -> Plan:
    for k, v in fields.items():
        if k in _EDITABLE_FIELDS:
            setattr(plan, k, v)
    db.commit()
    db.refresh(plan)
    invalidate_plan_cache()
    return plan


def create_plan(db: Session, slug: str, fields: dict[str, Any]) -> Plan:
    plan = Plan(slug=slug, name=fields.get("name") or slug.title())
    for k, v in fields.items():
        if k in _EDITABLE_FIELDS:
            setattr(plan, k, v)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    invalidate_plan_cache()
    return plan


def delete_plan(db: Session, plan: Plan) -> None:
    """Soft-delete: free plan refuses to be removed; paid plans get
    is_active=false instead of an actual DELETE so historical Payment
    rows keep their FK valid."""
    if plan.is_free:
        raise ValueError("free plan cannot be deleted")
    plan.is_active = False
    db.commit()
    invalidate_plan_cache()
