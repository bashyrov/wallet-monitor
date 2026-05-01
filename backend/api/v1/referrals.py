"""Referral program HTTP API.

User-facing:
- GET  /referrals/me        — code, link, totals, history
- POST /referrals/me/payout — submit payout request (TRC20 USDT)

Admin-facing:
- GET  /admin/referrals/payouts          — list payout requests (filter by status)
- POST /admin/referrals/payouts/{id}/pay — mark a request paid (note = tx hash)
- POST /admin/referrals/payouts/{id}/reject — mark a request rejected
- PATCH /admin/users/{id}/referral-pct — already in admin.py (override pct)

Earnings are written by payment_service._activate_user — never via this API.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.api.deps import get_admin_user, get_current_user, get_db
from backend.db.models import (
    Payment,
    ReferralEarning,
    ReferralPayoutRequest,
    User,
)
from backend.services import audit_log, referral_service

router = APIRouter(prefix="/referrals", tags=["referrals"])
admin_router = APIRouter(prefix="/admin/referrals", tags=["admin-referrals"])
logger = logging.getLogger("avalant.referrals")


def _public_link(code: str, request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    if not base:
        return f"/register?ref={code}"
    return f"{base}/register?ref={code}"


@router.get("/me")
def my_referral(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    code = referral_service.ensure_referral_code(db, user)
    db.commit()

    pct = referral_service.get_commission_pct(user)
    earned = referral_service.total_earned(db, user)
    paid = referral_service.total_paid(db, user)
    pending = referral_service.total_pending(db, user)
    available = earned - paid - pending
    refs = referral_service.referee_count(db, user)

    history_rows = (
        db.query(ReferralEarning)
        .filter(ReferralEarning.referrer_id == user.id)
        .order_by(ReferralEarning.created_at.desc())
        .limit(50)
        .all()
    )
    history = [
        {
            "id": r.id,
            "amount_usd": float(r.amount_usd),
            "pct": r.pct,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in history_rows
    ]
    payouts_rows = (
        db.query(ReferralPayoutRequest)
        .filter(ReferralPayoutRequest.user_id == user.id)
        .order_by(ReferralPayoutRequest.created_at.desc())
        .limit(50)
        .all()
    )
    payouts = [
        {
            "id": p.id,
            "amount_usd": float(p.amount_usd),
            "address": p.address,
            "status": p.status,
            "note": p.note,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "resolved_at": p.resolved_at.isoformat() if p.resolved_at else None,
        }
        for p in payouts_rows
    ]
    return {
        "code": code,
        "link": _public_link(code, request),
        "commission_pct": pct,
        "payout_address": user.referral_payout_address,
        "totals": {
            "earned_usd": float(earned),
            "paid_usd": float(paid),
            "pending_usd": float(pending),
            "available_usd": float(available),
            "referees": refs,
        },
        "history": history,
        "payouts": payouts,
    }


class PayoutRequestBody(BaseModel):
    amount_usd: float
    address: str


@router.post("/me/payout", status_code=201)
def request_payout(
    body: PayoutRequestBody,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not referral_service.verify_trc20_address(body.address):
        raise HTTPException(400, "Invalid TRC20 address")
    try:
        amount = Decimal(str(body.amount_usd)).quantize(Decimal("0.01"))
    except Exception:
        raise HTTPException(400, "Invalid amount")
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    available = referral_service.available_balance(db, user)
    if amount > available:
        raise HTTPException(
            400,
            f"Requested ${amount} exceeds available ${available}",
        )

    user.referral_payout_address = body.address.strip()
    db.add(user)

    req = ReferralPayoutRequest(
        user_id=user.id,
        amount_usd=amount,
        address=body.address.strip(),
        status="pending",
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    logger.info("Payout request: user=%s amount=%s addr=%s req=%s",
                user.id, amount, body.address, req.id)
    return {"id": req.id, "status": req.status, "amount_usd": float(req.amount_usd)}


# ── Admin ──────────────────────────────────────────────────────────────────

@admin_router.get("/payouts")
def admin_list_payouts(
    status: Optional[str] = Query(None, pattern=r"^(pending|paid|rejected)$"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    q = db.query(ReferralPayoutRequest).order_by(ReferralPayoutRequest.created_at.desc())
    if status:
        q = q.filter(ReferralPayoutRequest.status == status)
    rows = q.limit(500).all()
    out = []
    for r in rows:
        u = db.query(User.username, User.email).filter(User.id == r.user_id).first()
        out.append({
            "id": r.id,
            "user_id": r.user_id,
            "username": u.username if u else None,
            "email": u.email if u else None,
            "amount_usd": float(r.amount_usd),
            "address": r.address,
            "status": r.status,
            "note": r.note,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        })
    return {"payouts": out}


class AdminResolveBody(BaseModel):
    note: Optional[str] = None


def _resolve_payout(
    db: Session,
    request: Request,
    admin: User,
    payout_id: int,
    new_status: str,
    note: Optional[str],
) -> ReferralPayoutRequest:
    p = (
        db.query(ReferralPayoutRequest)
        .filter(ReferralPayoutRequest.id == payout_id)
        .first()
    )
    if not p:
        raise HTTPException(404, "Payout not found")
    if p.status != "pending":
        raise HTTPException(409, f"Payout already resolved ({p.status})")
    p.status = new_status
    p.note = note
    p.resolved_at = datetime.utcnow()
    db.add(p)
    db.commit()
    db.refresh(p)
    audit_log.record(
        db, request,
        actor=admin,
        action=f"referral.payout.{new_status}",
        target_type="referral_payout",
        target_id=p.id,
        delta={"amount_usd": float(p.amount_usd), "address": p.address, "note": note},
    )
    logger.info("admin %s resolved payout %s -> %s (note=%s)",
                admin.username, p.id, new_status, note)
    return p


@admin_router.post("/payouts/{payout_id}/pay")
def admin_pay_payout(
    payout_id: int,
    body: AdminResolveBody,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    p = _resolve_payout(db, request, admin, payout_id, "paid", body.note)
    return {"id": p.id, "status": p.status, "note": p.note}


@admin_router.post("/payouts/{payout_id}/reject")
def admin_reject_payout(
    payout_id: int,
    body: AdminResolveBody,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    p = _resolve_payout(db, request, admin, payout_id, "rejected", body.note)
    return {"id": p.id, "status": p.status, "note": p.note}
