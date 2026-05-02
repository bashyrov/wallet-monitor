"""Referral program — code generation, commission credit, payout flow.

Money model
-----------
Every confirmed payment from a referee ($X paid after promo discount)
credits the referrer with a `ReferralEarning` of $X × pct/100. The earning
row is the source of truth — never recompute from `payments` directly.

Earnings live in one of two states:

- **Unclaimed**  (`payout_request_id IS NULL`): contributes to the
  available balance the user can withdraw.
- **Claimed**   (`payout_request_id = <id>`): linked to a payout request.
  - If that request is `pending` or `completed`, the earning is "spoken
    for" and never returns to available.
  - If the admin marks the request `cancelled`, every linked earning is
    unlinked and returns to available.

Available balance:
    sum(amount_usd) WHERE referrer_id = me AND payout_request_id IS NULL

Payout flow:

    POST /referrals/me/payout {address}
      - Reject if user has a pending payout (DB UNIQUE + app check).
      - amount = sum(unclaimed earnings)
      - Reject if amount < $100.
      - Create ReferralPayoutRequest(amount=amount, status='pending').
      - UPDATE referral_earnings SET payout_request_id=<new>
                                 WHERE referrer_id=me AND payout_request_id IS NULL.

    Admin clicks Complete  → status='completed' (link stays).
    Admin clicks Cancel    → status='cancelled' + unlink earnings.

Trust boundary
--------------
- `credit_commission` is called ONLY from `payment_service._activate_user`,
  which itself runs only after the CryptoCloud webhook signature passes.
- Idempotency: payment_id is UNIQUE on referral_earnings, so retried
  webhooks don't double-credit.
- Referral link (`users.referred_by_id`) is set ONCE at register time
  with self-referral guarded. There is NO API endpoint to change it.
- Commission rate is captured into `referral_earnings.pct` at credit
  time so admin overrides apply forward only — no retroactive rewriting
  of past earnings.
"""
from __future__ import annotations

import logging
import re
import secrets
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db.models import (
    Payment,
    ReferralEarning,
    ReferralPayoutRequest,
    User,
)

logger = logging.getLogger(__name__)


# Commission % new users earn per referee payment — admin can override
# per user via the `referral_pct_override` column.
DEFAULT_COMMISSION_PCT = 20.0
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid handoff errors
CODE_LENGTH = 7

# Default minimum withdrawal — used as the fallback when admin_settings
# can't be reached (test isolation, cold cache). Live value is read via
# `current_min_payout_usd()` so admins can tune it from /admin without
# a deploy. The floor / ceiling [1, 10000] is enforced inside
# admin_settings.get_referral_min_payout_usd to defend against typos.
DEFAULT_MIN_PAYOUT_USD = Decimal("100.00")

# Lifecycle. Only these strings are written by the service.
PAYOUT_PENDING = "pending"
PAYOUT_COMPLETED = "completed"
PAYOUT_CANCELLED = "cancelled"
_VALID_STATUSES = (PAYOUT_PENDING, PAYOUT_COMPLETED, PAYOUT_CANCELLED)


def current_min_payout_usd() -> Decimal:
    """Live minimum-payout floor. Admin-configurable via
    /admin → Communications → Referral. Cached 15s by admin_settings."""
    try:
        from backend.services import admin_settings
        return Decimal(str(admin_settings.get_referral_min_payout_usd()))
    except Exception:
        return DEFAULT_MIN_PAYOUT_USD


# Tron base58check addresses begin with T and are 34 chars long.
# Strict shape check; we don't decode-verify here.
_TRC20_RE = re.compile(r"^T[A-HJ-NP-Z1-9a-km-z]{33}$")


def verify_trc20_address(addr: str) -> bool:
    if not isinstance(addr, str):
        return False
    return bool(_TRC20_RE.match(addr.strip()))


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def ensure_referral_code(db: Session, user: User) -> str:
    """Mint a unique code for the user if they don't have one.

    Idempotent — never overwrites an existing code (the user's existing
    code is publicly known via their share link, so changing it would
    silently break already-distributed links)."""
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


# ── Credit (called from webhook activation only) ────────────────────────────

def credit_commission(
    db: Session,
    *,
    referee: User,
    payment: Payment,
    amount_usd: Decimal | float,
) -> Optional[ReferralEarning]:
    """Credit a commission row for a referee's confirmed payment.

    No-op if the referee has no referrer, the payment already has an
    earning row, or the amount is non-positive. Never raises on those
    paths — the activation flow swallows referral failures so a buggy
    bookkeeping path can't block plan delivery.
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
    # Self-referral guard: redundant — register-time blocks self-link —
    # but cheap to keep here so a bad SQL fix can't trigger commission.
    if referrer.id == referee.id:
        return None
    # Idempotency: payment_id is UNIQUE in the schema, but check
    # explicitly so we don't burn a transaction on a unique-violation.
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


# ── Reads ───────────────────────────────────────────────────────────────────

def total_earned(db: Session, user: User) -> Decimal:
    """Total commissions ever credited to this user. Includes paid +
    pending + available — i.e., gross lifetime."""
    val = (
        db.query(func.coalesce(func.sum(ReferralEarning.amount_usd), 0))
        .filter(ReferralEarning.referrer_id == user.id)
        .scalar()
    )
    return Decimal(val or 0)


def total_paid(db: Session, user: User) -> Decimal:
    """Sum of payouts that admin has marked as completed."""
    val = (
        db.query(func.coalesce(func.sum(ReferralPayoutRequest.amount_usd), 0))
        .filter(
            ReferralPayoutRequest.user_id == user.id,
            ReferralPayoutRequest.status == PAYOUT_COMPLETED,
        )
        .scalar()
    )
    return Decimal(val or 0)


def total_pending(db: Session, user: User) -> Decimal:
    """Sum of payouts currently awaiting admin review."""
    val = (
        db.query(func.coalesce(func.sum(ReferralPayoutRequest.amount_usd), 0))
        .filter(
            ReferralPayoutRequest.user_id == user.id,
            ReferralPayoutRequest.status == PAYOUT_PENDING,
        )
        .scalar()
    )
    return Decimal(val or 0)


def available_balance(db: Session, user: User) -> Decimal:
    """Money the user can request to withdraw RIGHT NOW.

    Computed directly from unclaimed earnings rather than via
    earned − paid − pending so the schema is the source of truth and
    the two paths can never disagree.
    """
    val = (
        db.query(func.coalesce(func.sum(ReferralEarning.amount_usd), 0))
        .filter(
            ReferralEarning.referrer_id == user.id,
            ReferralEarning.payout_request_id.is_(None),
        )
        .scalar()
    )
    return Decimal(val or 0)


def referee_count(db: Session, user: User) -> int:
    return db.query(func.count(User.id)).filter(User.referred_by_id == user.id).scalar() or 0


def has_pending_payout(db: Session, user: User) -> bool:
    return (
        db.query(ReferralPayoutRequest.id)
        .filter(
            ReferralPayoutRequest.user_id == user.id,
            ReferralPayoutRequest.status == PAYOUT_PENDING,
        )
        .first()
        is not None
    )


def list_unclaimed_earnings(db: Session, user: User) -> list[ReferralEarning]:
    return (
        db.query(ReferralEarning)
        .filter(
            ReferralEarning.referrer_id == user.id,
            ReferralEarning.payout_request_id.is_(None),
        )
        .order_by(ReferralEarning.created_at.asc())
        .all()
    )


def list_earnings_for_payout(
    db: Session,
    payout: ReferralPayoutRequest,
) -> list[ReferralEarning]:
    return (
        db.query(ReferralEarning)
        .filter(ReferralEarning.payout_request_id == payout.id)
        .order_by(ReferralEarning.created_at.asc())
        .all()
    )


# ── Payout writes ───────────────────────────────────────────────────────────

class PayoutError(ValueError):
    """Raised when a payout request fails business rules. The HTTP layer
    converts this into a 4xx with the message body."""
    pass


def request_payout(
    db: Session,
    *,
    user: User,
    address: str,
) -> ReferralPayoutRequest:
    """Create a new payout request claiming every unclaimed earning.

    Rules enforced (in order — the first failing one is reported):
    - Address shape must be a valid TRC20 (T + 33 base58 chars).
    - User must have no other payout currently in `pending` state.
    - Sum of unclaimed earnings must be ≥ MIN_PAYOUT_USD.

    On success: a `pending` payout row is created, and every unclaimed
    `referral_earnings` row for this user is linked to it. The two
    operations happen in the same transaction so a partial state is
    impossible — the DB UNIQUE on (user_id) WHERE status='pending'
    prevents two parallel requests from both inserting.
    """
    if not verify_trc20_address(address):
        raise PayoutError("Invalid TRC20 address")
    if has_pending_payout(db, user):
        raise PayoutError("You already have a pending payout request")

    amount = available_balance(db, user)
    min_payout = current_min_payout_usd()
    if amount < min_payout:
        raise PayoutError(f"Minimum payout is ${min_payout}")

    addr = address.strip()
    user.referral_payout_address = addr
    db.add(user)

    req = ReferralPayoutRequest(
        user_id=user.id,
        amount_usd=amount,
        address=addr,
        status=PAYOUT_PENDING,
    )
    db.add(req)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise PayoutError("You already have a pending payout request")

    # Claim every unclaimed earning for this user. Single UPDATE so it's
    # atomic with the row insert above — no window where the request
    # exists without its earnings linked.
    db.execute(
        update(ReferralEarning)
        .where(
            ReferralEarning.referrer_id == user.id,
            ReferralEarning.payout_request_id.is_(None),
        )
        .values(payout_request_id=req.id)
    )
    db.commit()
    db.refresh(req)
    logger.info(
        "Payout request: user=%s amount=%s addr=%s req=%s",
        user.id, amount, addr, req.id,
    )
    return req


def admin_complete_payout(
    db: Session,
    *,
    payout_id: int,
    note: Optional[str] = None,
) -> ReferralPayoutRequest:
    """Mark a pending payout as completed. Called from admin handler.

    The earnings linked to this payout STAY linked — they're paid out,
    not in the available pool. Idempotency: a payout already in a
    terminal state is rejected (409) so an admin double-click can't
    trigger any side-effects again.
    """
    p = db.query(ReferralPayoutRequest).filter(ReferralPayoutRequest.id == payout_id).first()
    if not p:
        raise PayoutError("Payout not found")
    if p.status != PAYOUT_PENDING:
        raise PayoutError(f"Payout already resolved ({p.status})")
    from datetime import datetime
    p.status = PAYOUT_COMPLETED
    p.note = note
    p.resolved_at = datetime.utcnow()
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def admin_cancel_payout(
    db: Session,
    *,
    payout_id: int,
    note: Optional[str] = None,
) -> ReferralPayoutRequest:
    """Mark a pending payout as cancelled and return its earnings to the
    available pool.

    The unlink is a single UPDATE in the same transaction as the status
    flip — same atomicity guarantee as request_payout. After this, the
    user can submit a new payout request and the same earnings get
    claimed again.
    """
    p = db.query(ReferralPayoutRequest).filter(ReferralPayoutRequest.id == payout_id).first()
    if not p:
        raise PayoutError("Payout not found")
    if p.status != PAYOUT_PENDING:
        raise PayoutError(f"Payout already resolved ({p.status})")
    from datetime import datetime
    p.status = PAYOUT_CANCELLED
    p.note = note
    p.resolved_at = datetime.utcnow()
    db.add(p)
    # Unlink — earnings flow back to "unclaimed" and the user's
    # available_balance jumps back up.
    db.execute(
        update(ReferralEarning)
        .where(ReferralEarning.payout_request_id == p.id)
        .values(payout_request_id=None)
    )
    db.commit()
    db.refresh(p)
    return p
