"""Public + user-facing billing routes — plans, checkout, promos, popups.

Admin CRUD for these resources lives in `admin.py` under `/api/admin/...`.

Endpoints:
  GET  /api/plans                       — public, list active plans
  POST /api/payments/checkout           — auth, create CryptoCloud invoice
  POST /api/payments/cryptocloud/webhook — public, signed by CryptoCloud
  GET  /api/payments/me                 — auth, this user's payment history
  POST /api/promo/validate              — auth, dry-run promo against a plan
  GET  /api/popups/pending              — auth, eligible popups for me
  POST /api/popups/{id}/dismiss         — auth, mark a popup as dismissed
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.api.deps import get_db, get_current_user
from backend.db.models import User
from backend.services import (
    plan_service,
    payment_service,
    popup_service,
    promo_service,
    billing_period_service,
)

logger = logging.getLogger("avalant.billing")

router = APIRouter(tags=["billing"])


# ── Plans (public) ────────────────────────────────────────────────────────────
@router.get("/plans")
def list_plans(db: Session = Depends(get_db)) -> dict[str, Any]:
    plans = plan_service.list_plans(db, only_active=True)
    periods = billing_period_service.list_periods(db, only_active=True)
    return {
        "plans": [plan_service.serialize_plan(p) for p in plans],
        "billing_periods": [billing_period_service.serialize(p) for p in periods],
    }


# ── Checkout / payments ───────────────────────────────────────────────────────
@router.post("/payments/checkout")
async def checkout(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    plan_id = body.get("plan_id")
    billing_period_id = body.get("billing_period_id")
    promo_code = body.get("promo_code") or None
    if not plan_id or not billing_period_id:
        raise HTTPException(status_code=422, detail="plan_id and billing_period_id are required")
    try:
        return await payment_service.create_checkout(
            db, current_user, int(plan_id), int(billing_period_id), promo_code,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # Misconfigured CryptoCloud / network failure — log + 502.
        logger.error("checkout failed: %s", e)
        raise HTTPException(status_code=502, detail="Payment provider unavailable")


@router.post("/payments/cryptocloud/webhook")
async def cryptocloud_webhook(request: Request, db: Session = Depends(get_db)):
    """CryptoCloud posts JSON like
        {"status":"success","invoice_id":"...","token":"<jwt>", ...}
    We verify the JWT against our shop secret, then move the payment
    state forward. Any unauthenticated hit returns 401.
    """
    try:
        payload = await request.json()
    except Exception:
        # CryptoCloud sometimes sends form-encoded; fall back.
        form = await request.form()
        payload = dict(form)
    token = payload.get("token") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not payment_service.verify_webhook_signature(token):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    invoice_id = (
        payload.get("invoice_id") or payload.get("uuid") or payload.get("id")
    )
    status = payload.get("status") or payload.get("invoice_status") or "paid"
    if not invoice_id:
        raise HTTPException(status_code=422, detail="invoice_id missing")
    payment = payment_service.verify_and_apply_webhook(db, str(invoice_id), str(status), payload)
    return {"ok": True, "payment_id": payment.id if payment else None}


@router.get("/payments/me")
def my_payments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = payment_service.list_user_payments(db, current_user.id)
    return {"payments": [payment_service.serialize_payment(p) for p in rows]}


# ── Promo codes (public, auth-gated for sanity) ───────────────────────────────
@router.post("/promo/validate")
def promo_validate(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    code = (body.get("code") or "").strip()
    plan_id = body.get("plan_id")
    billing_period_id = body.get("billing_period_id")
    if not code or not plan_id or not billing_period_id:
        raise HTTPException(status_code=422, detail="code, plan_id, billing_period_id are required")
    promo = promo_service.validate_for_plan(db, code, int(plan_id))
    if not promo:
        return {"valid": False}
    plan = plan_service.get_plan(db, int(plan_id))
    period = billing_period_service.get_period(db, int(billing_period_id))
    if not plan or not period:
        return {"valid": False}
    pricing = payment_service.compute_pricing(plan, period, promo)
    return {
        "valid": True,
        "code": promo.code,
        "discount_pct": float(promo.discount_pct),
        "base_amount_usd": float(pricing["base_amount_usd"]),
        "final_amount_usd": float(pricing["final_amount_usd"]),
    }


# ── Popups ────────────────────────────────────────────────────────────────────
@router.get("/popups/pending")
def popups_pending(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return {"popups": popup_service.get_pending_for_user(db, current_user)}


@router.post("/popups/{popup_id}/dismiss")
def popup_dismiss(
    popup_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ok = popup_service.dismiss(db, popup_id, current_user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="popup not found")
    return {"ok": True}
