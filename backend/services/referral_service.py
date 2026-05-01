"""Referral program — code generation + commission credit + payout balance.

What this service owns:
- code generation (`ensure_referral_code`)
- commission rate read with admin override (`get_commission_pct`)
- crediting commissions on confirmed payment activations (`credit_commission`)
  — called from payment_service._activate_user, NEVER from cart/intent paths
- balance arithmetic (`available_balance`) for payout-request UI + admin UI
- TRC20 address validation for payout addresses

The "validation" the user asked for is structural:
1. credit only on signature-verified webhook activations (caller side)
2. one earning row per payment (UNIQUE constraint on payments.id)
3. user can never request more than (sum of earnings) − (paid + pending payouts)
4. admin can override commission per user but never retroactively (override
   applies to *future* credits only)
"""
from __future__ import annotations

import logging
import re
import secrets
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.db.models import (
    Payment,
    ReferralEarning,
    ReferralPayoutRequest,
    User,
)

logger = logging.getLogger(__name__)


DEFAULT_COMMISSION_PCT = 20.0  # 20% — admin can override per user via referral_pct_override
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid handoff errors
CODE_LENGTH = 7


# Tron base58check addresses begin with T and are 34 chars long.
# Strict: no I/O/0/l (base58), but we don't decode-verify here — just shape.
_TRC20_RE = re.compile(r"^T[A-HJ-NP-Z1-9a-km-z]{33}$")


def verify_trc20_address(addr: str) -> bool:
    if not isinstance(addr, str):
        return False
    addr = addr.strip()
    return bool(_TRC20_RE.match(addr))


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def ensure_referral_code(db: Session, user: User) -> str:
    """Mint a unique code for the user if they don't have one yet."""
    if user.referral_code:
        return user.referral_code
    for _ in range(8):
        candidate = generate_code()
        if db.query(User.id).filter(func.upper(User.referral_code) == candidate).first():
            continue
        user.referral_code = candidate
        db.add(user)
        db.flush()
        return candidate
    # Astronomically unlikely. Bubble up so callers can surface to the user.
    raise RuntimeError("could not allocate unique referral code after 8 tries")


def find_referrer_by_code(db: Session, code: str) -> Optional[User]:
    if not code:
        return None
    norm = code.strip().upper()
    if not norm:
        return None
    return (
        db.query(User)
        .filter(func.upper(User.referral_code) == norm)
        .first()
    )


def get_commission_pct(user: User) -> float:
    """Effective commission rate to credit when *this user* refers someone."""
    if user.referral_pct_override is not None:
        try:
            v = float(user.referral_pct_override)
        except (TypeError, ValueError):
            return DEFAULT_COMMISSION_PCT
        return max(0.0, min(100.0, v))
    return DEFAULT_COMMISSION_PCT


def credit_commission(
    db: Session,
    *,
    referee: User,
    payment: Payment,
    amount_usd: Decimal | float,
) -> Optional[ReferralEarning]:
    """Credit a commission row for the referee's payment.

    No-op if:
    - referee has no referrer
    - this payment already has an earning row
    - amount is non-positive

    Caller (payment_service) is the trust boundary — only call from the
    signature-verified webhook activation path.
    """
    if referee.referred_by_id is None or amount_usd is None:
        return None
    try:
        amount = Decimal(str(amount_usd))
    except Exception:  # noqa: BLE001
        return None
    if amount <= 0:
        return None
    referrer = db.query(User).filter(User.id == referee.referred_by_id).first()
    if not referrer:
        return None
    # Idempotency: payment_id is UNIQUE in the schema, but check explicitly so
    # we don't burn a transaction rollback on the unique-violation path.
    existing = (
        db.query(ReferralEarning)
        .filter(ReferralEarning.payment_id == payment.id)
        .first()
    )
    if existing:
        return existing

    pct = get_commission_pct(referrer)
    commission = (amount * Decimal(str(pct)) / Decimal("100")).quantize(Decimal("0.01"))
    if commission <= 0:
        return None
    row = ReferralEarning(
        referrer_id=referrer.id,
        referee_id=referee.id,
        payment_id=payment.id,
        pct=pct,
        amount_usd=commission,
    )
    db.add(row)
    db.flush()
    logger.info(
        "referral.credit referrer=%s referee=%s payment=%s pct=%.2f amount=%s",
        referrer.id, referee.id, payment.id, pct, commission,
    )
    return row


def total_earned(db: Session, user: User) -> Decimal:
    val = (
        db.query(func.coalesce(func.sum(ReferralEarning.amount_usd), 0))
        .filter(ReferralEarning.referrer_id == user.id)
        .scalar()
    )
    return Decimal(val or 0)


def total_paid(db: Session, user: User) -> Decimal:
    val = (
        db.query(func.coalesce(func.sum(ReferralPayoutRequest.amount_usd), 0))
        .filter(
            ReferralPayoutRequest.user_id == user.id,
            ReferralPayoutRequest.status == "paid",
        )
        .scalar()
    )
    return Decimal(val or 0)


def total_pending(db: Session, user: User) -> Decimal:
    val = (
        db.query(func.coalesce(func.sum(ReferralPayoutRequest.amount_usd), 0))
        .filter(
            ReferralPayoutRequest.user_id == user.id,
            ReferralPayoutRequest.status == "pending",
        )
        .scalar()
    )
    return Decimal(val or 0)


def available_balance(db: Session, user: User) -> Decimal:
    return total_earned(db, user) - total_paid(db, user) - total_pending(db, user)


def referee_count(db: Session, user: User) -> int:
    return db.query(func.count(User.id)).filter(User.referred_by_id == user.id).scalar() or 0
