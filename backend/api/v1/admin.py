from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.api.deps import get_admin_user, get_db
from backend.db.models import User, Wallet, Tag, ProviderErrorLog
from backend.plans import PLAN_LIMITS, VALID_PLANS, ADMIN_ONLY_PLANS, wallet_limit

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
