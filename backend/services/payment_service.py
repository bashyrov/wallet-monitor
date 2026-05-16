"""CryptoCloud payment lifecycle.

Flow:
  1. User clicks "Buy" on /checkout → POST /api/payments/checkout with
     {plan_id, billing_cycle, promo_code?}.
  2. We compute final_amount_usd = base × (1 - discount_pct/100), round
     to 2 decimals, persist a `payments` row with status='pending',
     then call CryptoCloud `/v2/invoice/create` to obtain an invoice URL.
  3. CryptoCloud hosts the payment page; we redirect the user there.
  4. CryptoCloud calls our webhook (status=paid) — we verify the JWT
     signature, mark `payments.status='paid'`, set `paid_at`, compute
     `activated_until = now + 30d / 365d`, and flip the user's
     `plan_id` + `plan_expires_at`.

CryptoCloud public docs: https://docs.cryptocloud.plus/

Webhook auth: each invoice carries a `token` (JWT signed with
SECRET_API_KEY). We verify with PyJWT and the configured shop secret.

All amounts in USD; CryptoCloud handles the FX conversion to the actual
crypto the user pays in.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx
from sqlalchemy.orm import Session

from backend.db.models import Payment, Plan, PromoCode, PromoCodeUsage, User, BillingPeriod
from backend.services import plan_service, promo_service, billing_period_service
from settings import settings

logger = logging.getLogger("avalant.payment_service")

CRYPTOCLOUD_API_BASE = "https://api.cryptocloud.plus"


# ── Money helpers ─────────────────────────────────────────────────────────────
def _round_money(amount: Decimal | float | int) -> Decimal:
    return Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_pricing(
    plan: Plan,
    period: BillingPeriod,
    promo: PromoCode | None,
) -> dict[str, Decimal]:
    """Server-authoritative price calculation.

    Pricing model:
      base_total = plan.price_usd_monthly × period.months × (1 - period.discount_pct / 100)
      final     = base_total × (1 - promo.discount_pct / 100)

    Both rounded to 2 decimals (HALF_UP) at the end so the user sees a
    clean number on the invoice page. Frontend may show any intermediate
    UI, but the actual money is computed here from the DB rows so a
    tampered request can't sneak through.
    """
    base = billing_period_service.compute_total(plan.price_usd_monthly, period)
    discount_pct = Decimal(promo.discount_pct) if promo else Decimal("0")
    final = _round_money(base * (Decimal("100") - discount_pct) / Decimal("100"))
    return {
        "base_amount_usd": base,
        "discount_pct": discount_pct,
        "final_amount_usd": final,
    }


def _activation_window(period: BillingPeriod) -> timedelta:
    return timedelta(days=int(period.months) * 30)


# ── CryptoCloud HTTP ──────────────────────────────────────────────────────────
def _cc_headers() -> dict[str, str]:
    if not settings.CRYPTOCLOUD_API_KEY:
        raise RuntimeError("CRYPTOCLOUD_API_KEY is not configured")
    return {
        "Authorization": f"Token {settings.CRYPTOCLOUD_API_KEY}",
        "Content-Type": "application/json",
    }


async def _cc_create_invoice(
    *,
    amount_usd: Decimal,
    order_id: str,
    description: str,
) -> dict[str, Any]:
    if not settings.CRYPTOCLOUD_SHOP_ID:
        raise RuntimeError("CRYPTOCLOUD_SHOP_ID is not configured")
    payload = {
        "shop_id": settings.CRYPTOCLOUD_SHOP_ID,
        "amount": float(amount_usd),
        "currency": "USD",
        "order_id": order_id,
        "description": description,
        "success_url": settings.CRYPTOCLOUD_SUCCESS_URL,
        "fail_url": settings.CRYPTOCLOUD_FAIL_URL,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{CRYPTOCLOUD_API_BASE}/v2/invoice/create",
            json=payload,
            headers=_cc_headers(),
        )
    if r.status_code >= 400:
        raise RuntimeError(f"cryptocloud invoice create failed {r.status_code}: {r.text}")
    data = r.json()
    if data.get("status") not in ("success", None):
        # API returns {"status": "error", "result": {...}} on rejection.
        raise RuntimeError(f"cryptocloud rejected: {data}")
    result = data.get("result") or data
    invoice_uuid = result.get("uuid") or result.get("invoice_id") or result.get("id")
    invoice_url = result.get("link") or result.get("url") or result.get("payment_url")
    if not invoice_uuid or not invoice_url:
        raise RuntimeError(f"cryptocloud response missing uuid/link: {result}")
    return {"uuid": str(invoice_uuid), "url": invoice_url, "raw": result}


# ── Checkout entrypoint ───────────────────────────────────────────────────────
async def create_checkout(
    db: Session,
    user: User,
    plan_id: int,
    billing_period_id: int,
    promo_code_str: str | None = None,
) -> dict[str, Any]:
    plan = plan_service.get_plan(db, plan_id)
    if not plan or not plan.is_active:
        raise ValueError("plan not found")
    if plan.is_free:
        raise ValueError("free plan does not require checkout")

    period = billing_period_service.get_period(db, billing_period_id)
    if not period or not period.is_active:
        raise ValueError("billing period not found")

    promo: PromoCode | None = None
    if promo_code_str:
        promo = promo_service.validate_for_plan(db, promo_code_str, plan.id, user_id=user.id)
        if not promo:
            raise ValueError("invalid promo code")

    pricing = compute_pricing(plan, period, promo)
    if pricing["final_amount_usd"] <= 0:
        raise ValueError("computed amount is zero — refusing to create invoice")

    payment = Payment(
        user_id=user.id,
        plan_id=plan.id,
        billing_period_id=period.id,
        billing_cycle=period.slug,
        base_amount_usd=pricing["base_amount_usd"],
        discount_pct=pricing["discount_pct"],
        final_amount_usd=pricing["final_amount_usd"],
        promo_code_id=promo.id if promo else None,
        provider="cryptocloud",
        status="pending",
    )
    db.add(payment)
    db.flush()  # need payment.id for order_id

    invoice = await _cc_create_invoice(
        amount_usd=pricing["final_amount_usd"],
        order_id=f"avalant-{payment.id}",
        description=f"Avalant {plan.name} · {period.label}",
    )
    payment.provider_invoice_id = invoice["uuid"]
    payment.provider_invoice_url = invoice["url"]
    db.commit()
    db.refresh(payment)
    return {
        "payment_id": payment.id,
        "invoice_url": invoice["url"],
        "amount_usd": float(pricing["final_amount_usd"]),
        "discount_pct": float(pricing["discount_pct"]),
    }


# ── Webhook handler ───────────────────────────────────────────────────────────
def _activate_user(db: Session, payment: Payment) -> None:
    # Refunded payments must never re-grant a plan, even if a stale
    # webhook reaches this code path. The admin button + the webhook
    # branch both guard upstream, but this is the canonical place.
    if payment.status == "refunded" or payment.refunded_at is not None:
        logger.warning(
            "_activate_user: refusing to activate refunded payment id=%s",
            payment.id,
        )
        return
    user = db.query(User).filter(User.id == payment.user_id).first()
    if not user:
        return
    user.plan_id = payment.plan_id
    plan = plan_service.get_plan(db, payment.plan_id)
    if plan:
        user.plan = plan.slug  # legacy mirror
    base_until = max(user.plan_expires_at or datetime.utcnow(), datetime.utcnow())
    period = None
    if payment.billing_period_id:
        period = billing_period_service.get_period(db, payment.billing_period_id)
    if period:
        payment.activated_until = base_until + _activation_window(period)
    else:
        # Legacy payment with no billing_period_id — fall back to 30 days
        # so we don't accidentally hand out a free year.
        payment.activated_until = base_until + timedelta(days=30)
    # Promo bonus days — granted on top of the regular billing-period window.
    # A 3-month plan + EARLY7 (7 bonus days) = 97 days of access. Stays
    # honest after server-side validate_for_plan, so bonus days only land
    # for promos that were actually eligible at checkout time.
    if payment.promo_code_id:
        from backend.db.models import PromoCode as _PC
        promo = db.query(_PC).filter(_PC.id == payment.promo_code_id).first()
        bonus = int(getattr(promo, "bonus_days", 0) or 0)
        if bonus > 0:
            payment.activated_until = payment.activated_until + timedelta(days=bonus)
            logger.info("promo bonus_days=%d added to payment %s (user=%s)",
                        bonus, payment.id, payment.user_id)
    user.plan_expires_at = payment.activated_until
    # Referral commission — credit the referrer (if any) for this confirmed
    # activation. Idempotent (UNIQUE on payment_id). Errors don't block the
    # plan activation: we'd rather honour the user's payment than reject it
    # because of a referral-bookkeeping bug.
    try:
        from backend.services import referral_service
        referral_service.credit_commission(
            db,
            referee=user,
            payment=payment,
            amount_usd=payment.final_amount_usd or 0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("referral credit failed for payment=%s user=%s: %s",
                       payment.id, user.id, exc)
    db.commit()
    # If the new plan is _smaller_ than the previous one (rare but
    # possible: admin manually flipped plan_id then user paid for a
    # cheaper tier), enforce the wallet cap right here so the user's
    # next /balance call doesn't 402 and also doesn't run on stale data.
    try:
        from backend.services import wallet_quota as _wq
        _wq.enforce_for_user(db, user)
    except Exception:
        pass
    # Plan-row cache flush — webhook just promoted the user to a new plan,
    # /me must see the new limits immediately, not after the 60s TTL.
    try:
        from backend.services.plan_service import invalidate_plan_cache
        invalidate_plan_cache()
    except Exception:
        pass
    # /api/auth/me has its own 30s per-user cache (auth.py:_ME_CACHE).
    # Flush this user's entry — иначе after-payment frontend ещё 30 секунд
    # видит старый plan limit.
    try:
        from backend.api.v1.auth import _invalidate_me_cache
        _invalidate_me_cache(user.id)
    except Exception:
        pass
    # Admin push-notification — fire-and-forget, never blocks the webhook.
    try:
        from backend.services.admin_alert_service import alert_payment
        amount = float(payment.final_amount_usd or 0)
        slug = (plan.slug if plan else (user.plan or "?"))
        alert_payment(user, slug, amount)
    except Exception:
        pass


def verify_and_apply_webhook(
    db: Session,
    invoice_uuid: str,
    status: str,
    raw: dict[str, Any],
) -> Payment | None:
    """Webhook lifecycle. CryptoCloud sends status='paid' on success,
    'partial'/'canceled'/'failed' otherwise. We tolerate unknown statuses
    by leaving the payment in 'pending' so a manual reconcile can fix it.

    Idempotency: SELECT ... FOR UPDATE locks the payments row so two
    concurrent webhook deliveries can't both pass the
    `if payment.status == "paid": return` guard and double-extend
    `plan_expires_at`. With a single-row lock the second webhook
    serialises behind the first and exits cleanly on the early-return
    branch.
    """
    payment = (
        db.query(Payment)
        .filter(Payment.provider_invoice_id == invoice_uuid)
        .with_for_update()
        .first()
    )
    if not payment:
        logger.warning("webhook for unknown invoice %s — ignoring", invoice_uuid)
        return None
    s = (status or "").lower()
    now = datetime.utcnow()
    if s in ("paid", "success"):
        if payment.status == "paid":
            # Idempotent — repeated webhook delivery shouldn't double-extend.
            return payment
        if payment.status == "refunded":
            # Defensive: never re-activate a refunded payment, even if
            # the provider sends a stale "paid" webhook out of order.
            logger.warning(
                "webhook tried to mark refunded payment %s as paid — ignoring",
                payment.id,
            )
            return payment
        payment.status = "paid"
        payment.paid_at = now
        # Promo usage ledger + counter bump.
        if payment.promo_code_id:
            promo = (
                db.query(PromoCode)
                .filter(PromoCode.id == payment.promo_code_id)
                .first()
            )
            if promo:
                db.add(PromoCodeUsage(
                    promo_code_id=promo.id,
                    user_id=payment.user_id,
                    payment_id=payment.id,
                    plan_id=payment.plan_id,
                    discount_pct=payment.discount_pct,
                ))
                promo.used_count = (promo.used_count or 0) + 1
        _activate_user(db, payment)
    elif s in ("canceled", "cancelled", "expired", "failed"):
        if payment.status == "pending":
            payment.status = "failed" if s == "failed" else "expired"
            db.commit()
    elif s in ("refunded", "chargeback", "reversed"):
        # Provider-initiated refund — same code path as the admin button.
        # Idempotent (refund_payment is a no-op when already refunded).
        if payment.status == "paid":
            try:
                refund_payment(db, payment, reason=f"webhook:{s}")
            except Exception as exc:
                logger.error("webhook refund failed for payment %s: %s", payment.id, exc)
        elif payment.status not in ("refunded",):
            # Pending/failed/expired payment getting "refunded" from the
            # provider is suspicious but harmless — just log + ignore.
            logger.info(
                "webhook %s for non-paid payment %s (status=%s) — ignoring",
                s, payment.id, payment.status,
            )
    else:
        logger.info("webhook unknown status %s for invoice %s — leaving pending", s, invoice_uuid)
    return payment


# ── Refund flow ─────────────────────────────────────────────────────────────

def refund_payment(
    db: Session,
    payment: Payment,
    *,
    reason: str | None = None,
) -> dict:
    """Mark a paid payment as refunded.

    Idempotent: returns early if already refunded.

    Side-effects:
    - payment.status = 'refunded', refunded_at + refunded_reason stamped
    - User's subscription is annulled (plan_expires_at = now). Auto-renew
      cleared. The user falls back to free-tier limits via plan_service
      on the next request — `_activate_user` refuses to re-grant a plan
      from a `refunded` payment, so a stale provider webhook can't undo
      the refund.
    - referral_service.reverse_commission runs to recoup / offset the
      partner's commission credit (if any).

    Caller should already have admin privileges; this function trusts
    its inputs.
    """
    out = {"payment_id": payment.id, "skipped": False}
    if payment.status == "refunded":
        out["skipped"] = True
        out["reason"] = "already-refunded"
        return out
    if payment.status != "paid":
        # Defensive: only paid payments can be refunded. A pending /
        # failed / expired payment never granted anything to refund.
        out["skipped"] = True
        out["reason"] = f"not-paid (status={payment.status})"
        return out

    from datetime import datetime as _dt
    now = _dt.utcnow()
    payment.status = "refunded"
    payment.refunded_at = now
    payment.refunded_reason = (reason or "")[:500]
    db.add(payment)

    # Annul the user's subscription. We don't try to be clever about
    # other paid payments the user may have — the admin can re-extend
    # via /admin/users/{id}/plan if the refund was for one cycle and
    # the user paid for two. Default behaviour is "this payment's plan
    # access is gone".
    try:
        user = db.query(User).filter(User.id == payment.user_id).first()
        if user is not None:
            user.plan_expires_at = now    # immediate expiry → free tier
            user.auto_renew = False
            db.add(user)
    except Exception as exc:
        logger.warning("refund: failed to annul user plan_id=%s: %s", payment.user_id, exc)

    db.flush()  # ensure payment row + user changes visible to reverse_commission

    # Recoup / offset the partner's commission. May write a sibling
    # negative earning, may adjust or cancel a pending payout, or do
    # nothing if the partner already withdrew via a completed payout.
    try:
        from backend.services import referral_service
        report = referral_service.reverse_commission(
            db, payment_id=payment.id, reason=reason,
        )
        out["referral_action"] = report.get("action")
        if "payout_id" in report:
            out["referral_payout_id"] = report["payout_id"]
    except Exception as exc:
        logger.error("refund: referral reversal failed for payment=%s: %s", payment.id, exc)
        out["referral_action"] = "error"

    db.commit()
    db.refresh(payment)

    # Plan-row cache flush so /me sees the user back on the free tier
    # immediately (no 60s wait).
    try:
        from backend.services.plan_service import invalidate_plan_cache
        invalidate_plan_cache()
    except Exception:
        pass

    logger.info(
        "refund: payment=%s user=%s amount=%s reason=%s referral=%s",
        payment.id, payment.user_id, payment.final_amount_usd, reason,
        out.get("referral_action"),
    )
    return out


def verify_webhook_signature(token: str | None) -> bool:
    """CryptoCloud signs each webhook with a JWT in the `token` field.
    We verify with the configured secret. Missing/invalid → False so
    the route returns 401 without touching DB."""
    if not token:
        return False
    if not settings.CRYPTOCLOUD_WEBHOOK_SECRET:
        # Misconfigured — refuse to accept anonymous webhooks.
        logger.error("CRYPTOCLOUD_WEBHOOK_SECRET unset, rejecting webhook")
        return False
    try:
        from jose import jwt
        jwt.decode(
            token,
            settings.CRYPTOCLOUD_WEBHOOK_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        return True
    except Exception as exc:
        logger.warning("webhook signature invalid: %s", exc)
        return False


def list_user_payments(db: Session, user_id: int, limit: int = 25) -> list[Payment]:
    return (
        db.query(Payment)
        .filter(Payment.user_id == user_id)
        .order_by(Payment.created_at.desc())
        .limit(limit)
        .all()
    )


def serialize_payment(p: Payment) -> dict[str, Any]:
    return {
        "id": p.id,
        "plan_id": p.plan_id,
        "billing_period_id": p.billing_period_id,
        "billing_cycle": p.billing_cycle,
        "base_amount_usd": float(p.base_amount_usd),
        "discount_pct": float(p.discount_pct or 0),
        "final_amount_usd": float(p.final_amount_usd),
        "status": p.status,
        "invoice_url": p.provider_invoice_url,
        "paid_at": p.paid_at.isoformat() if p.paid_at else None,
        "activated_until": p.activated_until.isoformat() if p.activated_until else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }
