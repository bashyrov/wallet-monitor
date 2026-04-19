from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.api.deps import get_admin_user, get_db
from backend.db.models import User, Wallet, Tag, ProviderErrorLog
from backend.plans import PLAN_LIMITS, VALID_PLANS, ADMIN_ONLY_PLANS, wallet_limit
from backend.services import admin_settings
from backend.services.arbitrage_service import FETCHERS

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/stats")
def admin_stats(
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    users_count    = db.query(func.count(User.id)).scalar()
    wallets_count  = db.query(func.count(Wallet.id)).scalar()
    tags_count     = db.query(func.count(Tag.id)).scalar()
    requests_total = db.query(func.sum(User.request_count)).scalar() or 0

    rows = (
        db.query(Wallet.wallet_type, Wallet.type_value, func.count(Wallet.id))
        .group_by(Wallet.wallet_type, Wallet.type_value)
        .all()
    )
    by_type: dict[str, dict] = {}
    for wtype, tval, cnt in rows:
        if wtype not in by_type:
            by_type[wtype] = {"count": 0, "providers": {}}
        by_type[wtype]["count"] += cnt
        by_type[wtype]["providers"][tval] = cnt

    recent = (
        db.query(User)
        .order_by(User.created_at.desc())
        .limit(10)
        .all()
    )
    recent_users = []
    for u in recent:
        wc = db.query(func.count(Wallet.id)).filter(Wallet.user_id == u.id).scalar()
        recent_users.append({
            "username": u.username,
            "email": u.email,
            "is_admin": u.is_admin,
            "wallets": wc,
            "last_active_at": u.last_active_at.strftime("%Y-%m-%d %H:%M") if u.last_active_at else None,
            "joined": u.created_at.strftime("%Y-%m-%d %H:%M"),
        })

    return {
        "users_count": users_count,
        "wallets_count": wallets_count,
        "tags_count": tags_count,
        "requests_total": requests_total,
        "by_type": by_type,
        "recent_users": recent_users,
    }


@router.get("/users")
def admin_list_users(
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    users = db.query(User).order_by(User.created_at).all()
    result = []
    for u in users:
        wc = db.query(func.count(Wallet.id)).filter(Wallet.user_id == u.id).scalar()
        last_active = u.last_active_at.strftime("%Y-%m-%d %H:%M") if u.last_active_at else None
        plan = getattr(u, 'plan', 'basic') or 'basic'
        expires = u.plan_expires_at.strftime("%Y-%m-%d") if getattr(u, 'plan_expires_at', None) else None
        result.append({
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "is_admin": u.is_admin,
            "is_blocked": getattr(u, 'is_blocked', False),
            "plan": plan,
            "plan_expires_at": expires,
            "wallet_limit": wallet_limit(plan),
            "request_count": getattr(u, 'request_count', 0),
            "last_active_at": last_active,
            "wallets": wc,
            "created_at": u.created_at.strftime("%Y-%m-%d %H:%M"),
        })
    return result


@router.get("/users/{user_id}")
def admin_get_user(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    wallets = db.query(Wallet).filter(Wallet.user_id == user_id).all()
    plan = getattr(user, 'plan', 'basic') or 'basic'
    expires = user.plan_expires_at.strftime("%Y-%m-%d") if getattr(user, 'plan_expires_at', None) else None
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_admin": user.is_admin,
        "is_blocked": getattr(user, "is_blocked", False),
        "plan": plan,
        "plan_expires_at": expires,
        "wallet_limit": wallet_limit(plan),
        "request_count": getattr(user, "request_count", 0),
        "last_active_at": user.last_active_at.strftime("%Y-%m-%d %H:%M") if user.last_active_at else None,
        "created_at": user.created_at.strftime("%Y-%m-%d %H:%M"),
        "wallets": [
            {"id": w.id, "name": w.name, "wallet_type": w.wallet_type, "type_value": w.type_value}
            for w in wallets
        ],
    }



@router.get("/provider-errors")
def provider_errors(
    n: int = Query(default=500, ge=1, le=10000),
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Return error counts grouped by provider, from the last N error rows."""
    subq = (
        db.query(ProviderErrorLog)
        .order_by(ProviderErrorLog.created_at.desc())
        .limit(n)
        .subquery()
    )
    rows = (
        db.query(
            subq.c.wallet_type,
            subq.c.type_value,
            subq.c.error_type,
            func.count().label("count"),
            func.max(subq.c.created_at).label("last_seen"),
        )
        .group_by(subq.c.wallet_type, subq.c.type_value, subq.c.error_type)
        .order_by(func.count().desc())
        .all()
    )
    total = db.query(func.count(ProviderErrorLog.id)).scalar() or 0
    return {
        "window": n,
        "total_stored": total,
        "rows": [
            {
                "wallet_type": r.wallet_type,
                "type_value":  r.type_value,
                "error_type":  r.error_type,
                "count":       r.count,
                "last_seen":   r.last_seen.strftime("%Y-%m-%d %H:%M") if r.last_seen else None,
            }
            for r in rows
        ],
    }


@router.patch("/users/{user_id}/block")
def toggle_block(
    user_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    if user_id == current_admin.id:
        raise HTTPException(status_code=400, detail="Cannot block yourself")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_blocked = not getattr(user, 'is_blocked', False)
    db.commit()
    return {"id": user.id, "username": user.username, "is_blocked": user.is_blocked}


from pydantic import BaseModel
from datetime import datetime as _dt


class _PlanBody(BaseModel):
    plan: str
    plan_expires_at: str | None = None


@router.patch("/users/{user_id}/plan")
def set_plan(
    user_id: int,
    body: _PlanBody,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    if body.plan not in VALID_PLANS:
        raise HTTPException(status_code=400, detail=f"Invalid plan. Valid: {', '.join(sorted(VALID_PLANS))}")
    if body.plan in ADMIN_ONLY_PLANS:
        target = db.query(User).filter(User.id == user_id).first()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if not target.is_admin:
            raise HTTPException(status_code=400, detail="Plan 'unlim' can only be assigned to admin users")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.plan = body.plan
    if body.plan_expires_at:
        try:
            user.plan_expires_at = _dt.strptime(body.plan_expires_at, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="plan_expires_at must be YYYY-MM-DD")
    else:
        user.plan_expires_at = None
    db.commit()
    plan = user.plan
    expires = user.plan_expires_at.strftime("%Y-%m-%d") if user.plan_expires_at else None
    return {
        "id": user.id,
        "username": user.username,
        "plan": plan,
        "plan_expires_at": expires,
        "wallet_limit": wallet_limit(plan),
    }


# ═══ Screener runtime controls ════════════════════════════════════════════════

class ScreenerConfigIn(BaseModel):
    hidden_symbols: list[str] | None = None
    disabled_exchanges: list[str] | None = None
    maintenance_mode: bool | None = None


@router.get("/screener-config")
def screener_config_get(_: User = Depends(get_admin_user)):
    hidden = sorted(admin_settings.get_hidden_symbols())
    disabled = sorted(admin_settings.get_disabled_exchanges())
    return {
        "hidden_symbols": hidden,
        "disabled_exchanges": disabled,
        "available_exchanges": sorted(FETCHERS.keys()),
        "maintenance_mode": admin_settings.is_maintenance(),
    }


@router.patch("/screener-config")
def screener_config_patch(
    body: ScreenerConfigIn,
    user: User = Depends(get_admin_user),
):
    known_ex = set(FETCHERS.keys())
    if body.hidden_symbols is not None:
        cleaned = sorted({str(s).strip().upper() for s in body.hidden_symbols if str(s).strip()})
        admin_settings.set_value(admin_settings.KEY_HIDDEN_SYMBOLS, cleaned, user_id=user.id)
    if body.disabled_exchanges is not None:
        cleaned_ex = sorted({
            str(s).strip().lower() for s in body.disabled_exchanges
            if str(s).strip().lower() in known_ex
        })
        admin_settings.set_value(admin_settings.KEY_DISABLED_EXCHANGES, cleaned_ex, user_id=user.id)
    if body.maintenance_mode is not None:
        admin_settings.set_value(admin_settings.KEY_MAINTENANCE, bool(body.maintenance_mode), user_id=user.id)
    return screener_config_get(user)


# ═══ Portfolio runtime controls ═══════════════════════════════════════════════

class PortfolioConfigIn(BaseModel):
    disabled_wallet_exchanges: list[str] | None = None
    disabled_chains: list[str] | None = None
    disabled_perpdexes: list[str] | None = None


def _portfolio_inventory() -> dict:
    from backend.providers.exchanges import EXCHANGE_PROVIDERS
    from backend.providers.perp_dexes import PERPDEX_PROVIDERS
    from backend.providers.chains import CHAIN_META
    return {
        "available_wallet_exchanges": sorted(
            v for v, p in EXCHANGE_PROVIDERS.items()
            if isinstance(p, type) and getattr(p, "enabled", True)
        ),
        "available_chains": sorted(
            v for v, m in CHAIN_META.items() if m.get("enabled", True)
        ),
        "available_perpdexes": sorted(
            v for v, p in PERPDEX_PROVIDERS.items()
            if isinstance(p, type) and getattr(p, "enabled", True)
        ),
    }


@router.get("/portfolio-config")
def portfolio_config_get(_: User = Depends(get_admin_user)):
    return {
        **_portfolio_inventory(),
        "disabled_wallet_exchanges": sorted(admin_settings.get_disabled_wallet_exchanges()),
        "disabled_chains": sorted(admin_settings.get_disabled_chains()),
        "disabled_perpdexes": sorted(admin_settings.get_disabled_perpdexes()),
    }


@router.patch("/portfolio-config")
def portfolio_config_patch(
    body: PortfolioConfigIn,
    user: User = Depends(get_admin_user),
):
    inv = _portfolio_inventory()
    if body.disabled_wallet_exchanges is not None:
        known = set(inv["available_wallet_exchanges"])
        cleaned = sorted({str(s).strip().lower() for s in body.disabled_wallet_exchanges if str(s).strip().lower() in known})
        admin_settings.set_value(admin_settings.KEY_DISABLED_WALLET_EXCHANGES, cleaned, user_id=user.id)
    if body.disabled_chains is not None:
        known = set(inv["available_chains"])
        cleaned = sorted({str(s).strip().lower() for s in body.disabled_chains if str(s).strip().lower() in known})
        admin_settings.set_value(admin_settings.KEY_DISABLED_CHAINS, cleaned, user_id=user.id)
    if body.disabled_perpdexes is not None:
        known = set(inv["available_perpdexes"])
        cleaned = sorted({str(s).strip().lower() for s in body.disabled_perpdexes if str(s).strip().lower() in known})
        admin_settings.set_value(admin_settings.KEY_DISABLED_PERPDEXES, cleaned, user_id=user.id)
    return portfolio_config_get(user)
