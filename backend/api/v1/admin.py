from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.api.deps import get_admin_user, get_db
from backend.db.models import User, Wallet, Tag

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/stats")
def admin_stats(
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    users_count   = db.query(func.count(User.id)).scalar()
    wallets_count = db.query(func.count(Wallet.id)).scalar()
    tags_count    = db.query(func.count(Tag.id)).scalar()

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
            "joined": u.created_at.strftime("%Y-%m-%d %H:%M"),
        })

    return {
        "users_count": users_count,
        "wallets_count": wallets_count,
        "tags_count": tags_count,
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
        result.append({
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "is_admin": u.is_admin,
            "is_blocked": getattr(u, 'is_blocked', False),
            "request_count": getattr(u, 'request_count', 0),
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
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_admin": user.is_admin,
        "is_blocked": getattr(user, "is_blocked", False),
        "request_count": getattr(user, "request_count", 0),
        "created_at": user.created_at.strftime("%Y-%m-%d %H:%M"),
        "wallets": [
            {"id": w.id, "name": w.name, "wallet_type": w.wallet_type, "type_value": w.type_value}
            for w in wallets
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
