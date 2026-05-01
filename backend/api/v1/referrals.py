"""Referral program HTTP API.

Surface area:
- GET  /referrals/me                        — code, link, totals, history
- POST /referrals/me/payout                 — submit payout (claims unclaimed)
- GET  /admin/referrals/payouts             — list payouts (filter by status)
- GET  /admin/referrals/payouts/{id}        — detail with linked earnings
- POST /admin/referrals/payouts/{id}/complete — admin marks completed
- POST /admin/referrals/payouts/{id}/cancel   — admin marks cancelled,
                                                returns earnings to user

Earnings rows are written by `payment_service._activate_user` only —
NEVER via this API. The user has no path to alter their own balance,
their `referred_by_id`, or their `referral_pct_override`.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.api.deps import get_admin_user, get_current_user, get_db
from backend.db.models import (
    Payment,
    Plan,
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


def _earning_card(db: Session, e: ReferralEarning) -> dict:
    """Compose one row for the user's history list — aggregates referee
    handle + plan name + paid amount so the UI doesn't have to round-trip."""
    referee = (
        db.query(User.username, User.email)
        .filter(User.id == e.referee_id)
        .first()
        if e.referee_id else None
    )
    payment = (
        db.query(Payment.final_amount_usd, Payment.plan_id)
        .filter(Payment.id == e.payment_id)
        .first()
        if e.payment_id else None
    )
    plan_slug = None
    if payment and payment.plan_id:
        p = db.query(Plan.slug).filter(Plan.id == payment.plan_id).first()
        plan_slug = p.slug if p else None
    paid_usd = None
    if payment:
        paid_usd = float(payment.final_amount_usd or 0)
    return {
        "id": e.id,
        "amount_usd": float(e.amount_usd),       # commission credited to me
        "pct": e.pct,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "referee": {
            "username": referee.username if referee else None,
            # email is intentionally hidden from /referrals/me — privacy.
            # Admin endpoint shows it; this user-facing one only shows
            # the public handle.
        },
        "plan": plan_slug,
        "referee_paid_usd": paid_usd,
        # `claimed` lets the UI grey-out earnings already linked to a
        # payout (pending OR completed).
        "claimed": e.payout_request_id is not None,
    }


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
    available = referral_service.available_balance(db, user)
    refs = referral_service.referee_count(db, user)

    history_rows = (
        db.query(ReferralEarning)
        .filter(ReferralEarning.referrer_id == user.id)
        .order_by(ReferralEarning.created_at.desc())
        .limit(100)
        .all()
    )
    history = [_earning_card(db, e) for e in history_rows]

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
        "min_payout_usd": float(referral_service.MIN_PAYOUT_USD),
        "payout_address": user.referral_payout_address,
        "totals": {
            "earned_usd": float(earned),
            "paid_usd": float(paid),
            "pending_usd": float(pending),
            "available_usd": float(available),
            "referees": refs,
        },
        "has_pending_payout": referral_service.has_pending_payout(db, user),
        "history": history,
        "payouts": payouts,
    }


class PayoutRequestBody(BaseModel):
    address: str


@router.post("/me/payout", status_code=201)
def request_payout(
    body: PayoutRequestBody,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Submit a payout request that claims every unclaimed earning.

    The amount field is server-computed — clients can't pick. This is the
    invariant that makes the balance arithmetic safe: the user can't ask
    for more than they've earned, can't ask for less either, and there's
    always a 1-to-1 correspondence between a payout's amount and the
    earnings it claimed.
    """
    try:
        req = referral_service.request_payout(db, user=user, address=body.address)
    except referral_service.PayoutError as e:
        # 409 for the "already pending" case so clients can render a
        # "wait for review" notice; everything else is 400.
        if "already" in str(e).lower():
            raise HTTPException(409, str(e))
        raise HTTPException(400, str(e))
    return {
        "id": req.id,
        "status": req.status,
        "amount_usd": float(req.amount_usd),
        "address": req.address,
        "created_at": req.created_at.isoformat() if req.created_at else None,
    }


# ── Admin ──────────────────────────────────────────────────────────────────

@admin_router.get("/payouts")
def admin_list_payouts(
    status: Optional[str] = Query(None, pattern=r"^(pending|completed|cancelled)$"),
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


@admin_router.get("/payouts/{payout_id}")
def admin_payout_detail(
    payout_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    """Detailed view: the payout itself + every earning linked to it.

    This is the screen the operator looks at before clicking Complete:
    they need to see exactly which referee transactions made up the
    requested amount. Email + plan + payment + commission per row."""
    p = db.query(ReferralPayoutRequest).filter(ReferralPayoutRequest.id == payout_id).first()
    if not p:
        raise HTTPException(404, "Payout not found")
    user_row = db.query(User.username, User.email).filter(User.id == p.user_id).first()

    earnings = referral_service.list_earnings_for_payout(db, p)
    earnings_out = []
    for e in earnings:
        ref = (
            db.query(User.id, User.username, User.email)
            .filter(User.id == e.referee_id)
            .first()
            if e.referee_id else None
        )
        payment = (
            db.query(Payment).filter(Payment.id == e.payment_id).first()
            if e.payment_id else None
        )
        plan_slug = None
        if payment and payment.plan_id:
            pl = db.query(Plan.slug).filter(Plan.id == payment.plan_id).first()
            plan_slug = pl.slug if pl else None
        earnings_out.append({
            "id": e.id,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "amount_usd": float(e.amount_usd),
            "pct": e.pct,
            "referee_id": ref.id if ref else None,
            "referee_username": ref.username if ref else None,
            "referee_email": ref.email if ref else None,
            "payment_id": e.payment_id,
            "referee_paid_usd": float(
                (payment.final_amount_usd if payment else None) or 0
            ),
            "plan": plan_slug,
        })
    sum_check = sum(x["amount_usd"] for x in earnings_out)
    return {
        "id": p.id,
        "user_id": p.user_id,
        "username": user_row.username if user_row else None,
        "email": user_row.email if user_row else None,
        "amount_usd": float(p.amount_usd),
        "address": p.address,
        "status": p.status,
        "note": p.note,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "resolved_at": p.resolved_at.isoformat() if p.resolved_at else None,
        # If sum_check ever drifts from amount_usd, an earning was edited
        # outside the service path — surface it so admin can investigate.
        "earnings_sum_check": sum_check,
        "earnings": earnings_out,
    }


class AdminResolveBody(BaseModel):
    note: Optional[str] = None


@admin_router.post("/payouts/{payout_id}/complete")
def admin_complete_payout(
    payout_id: int,
    body: AdminResolveBody,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    try:
        p = referral_service.admin_complete_payout(db, payout_id=payout_id, note=body.note)
    except referral_service.PayoutError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(404, msg)
        raise HTTPException(409, msg)
    audit_log.record(
        db, request=request, actor=admin, action="referral.payout.completed",
        target_type="referral_payout", target_id=p.id,
        delta={"amount_usd": float(p.amount_usd), "address": p.address, "note": body.note},
    )
    logger.info("admin %s completed payout %s (note=%s)", admin.username, p.id, body.note)
    return {"id": p.id, "status": p.status, "note": p.note}


@admin_router.post("/payouts/{payout_id}/cancel")
def admin_cancel_payout(
    payout_id: int,
    body: AdminResolveBody,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
):
    try:
        p = referral_service.admin_cancel_payout(db, payout_id=payout_id, note=body.note)
    except referral_service.PayoutError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(404, msg)
        raise HTTPException(409, msg)
    audit_log.record(
        db, request=request, actor=admin, action="referral.payout.cancelled",
        target_type="referral_payout", target_id=p.id,
        delta={"amount_usd": float(p.amount_usd), "address": p.address, "note": body.note},
    )
    logger.info("admin %s cancelled payout %s (note=%s)", admin.username, p.id, body.note)
    return {"id": p.id, "status": p.status, "note": p.note}
