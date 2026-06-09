"""Pure (FastAPI-free) helpers for the split-discount referral code system.

This module is the SINGLE business-logic surface for ReferralCode +
ReferralCodeRegistration + ReferralCodeUsage. Endpoints (admin + user)
are thin wrappers around these helpers; payment_service consumes them
from the webhook path. Pure ORM/typed-Python so it's unit-testable
without an HTTP layer.

Defense-in-depth contracts:

  1. CHECK constraints in the DB are the load-bearing security barrier
     (see migration r1s2t3u4v5w6 + model __table_args__). This service
     never tries to outsmart them — it raises clear errors BEFORE the
     CHECK would fire, so the user sees "pool > 25" instead of
     "IntegrityError". The CHECK is the backstop, not the primary gate.

  2. Cap policy lives here:
       * self_serve : commission + discount <= 25  (rejected at create)
       * admin      : commission + discount <= 45  (rejected at create)
       * 50 codes per owner  (anti-squatting)
       * 15 registrations per code  (anti-leak — 16th referee blocked)
       * 5 non-reversed usages per (code, referee)  (per-referee cap)

  3. The 5-cap counts NON-REVERSED usages only. Refund decrements (the
     reversed row is excluded by the WHERE clause), so the referee can
     buy again. This is the spec-mandated symmetry.

  4. Self-referral: owner_id != referee_id. Checked at registration time
     (not at code creation — owner can use their own ID in any read
     path, just can't bind as a referee).

  5. Code casing: stored as the user typed it ('CryptoPro'), unique on
     LOWER(code) (so 'Crypto' and 'CRYPTO' collide). Lookup is
     case-insensitive end-to-end.
"""
from __future__ import annotations
import re
import secrets
import string
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.db.models import (
    ReferralCode,
    ReferralCodeRegistration,
    ReferralCodeUsage,
    User,
)


# ── Constants (tunables — match the spec ════════════════════════════════════)

GLOBAL_CAP_PCT      = Decimal("45")     # commission + discount, ANY type
SELF_SERVE_CAP_PCT  = Decimal("25")     # commission + discount, self_serve only
CODES_PER_OWNER_MAX = 50                # anti-namespace-squat
REGISTRATIONS_CAP   = 15                # per code
USAGES_PER_REFEREE  = 5                 # per (code, referee), non-reversed

# Format: 4..32 chars, alphanumeric + - + _ (case preserved on store,
# unique on LOWER). Excludes whitespace, punctuation, emoji.
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,32}$")

# Auto-gen alphabet for system-suggested codes (skips 0/O/1/I/l for
# human-readability; matches the legacy users.referral_code 7-char style).
_GEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


# ── Errors ═══════════════════════════════════════════════════════════════════
# Service errors are plain exceptions; endpoints map them to HTTP codes.
# Distinct types so callers can branch (e.g. "owner cap" vs "global cap")
# without parsing strings.

class CodeServiceError(ValueError):
    """Base — all service-layer rejections inherit. Map to 400 by default."""
    pass


class CodeFormatError(CodeServiceError):
    """Code string fails the regex (length / charset)."""


class CodeTakenError(CodeServiceError):
    """LOWER(code) collides with an existing row."""


class PoolCapExceededError(CodeServiceError):
    """commission + discount exceeds the cap for the requested type."""


class OwnerCodeCapError(CodeServiceError):
    """Owner already has CODES_PER_OWNER_MAX codes."""


class RegistrationCapError(CodeServiceError):
    """Code has hit REGISTRATIONS_CAP — no new referees can bind."""


class SelfReferralError(CodeServiceError):
    """A user tried to register under their own code."""


class CodeNotFoundError(CodeServiceError):
    """LOWER(code) doesn't exist."""


# ── Helpers ══════════════════════════════════════════════════════════════════

def _to_decimal(v) -> Decimal:
    """Accept int / float / str / Decimal. Reject NaN / negative implicitly
    by the CHECK constraint later; here we only fail on parse error."""
    try:
        d = Decimal(str(v))
    except (InvalidOperation, TypeError):
        raise CodeFormatError(f"Invalid number: {v!r}")
    return d


def _validate_code_format(code: str) -> str:
    """Trim + format check. Returns the cleaned string (original casing)."""
    if not isinstance(code, str):
        raise CodeFormatError("Code must be a string")
    cleaned = code.strip()
    if not _CODE_RE.match(cleaned):
        raise CodeFormatError(
            "Code must be 4–32 chars, alphanumeric + '-' + '_' only"
        )
    return cleaned


def find_code_by_string(db: Session, raw: str) -> Optional[ReferralCode]:
    """Case-insensitive lookup. Returns None on miss — does NOT raise.
    Used by preview + registration paths where 'no match → no referral'
    is a normal flow (silent skip)."""
    if not raw or not isinstance(raw, str):
        return None
    lower = raw.strip().lower()
    if not lower:
        return None
    return (
        db.query(ReferralCode)
        .filter(func.lower(ReferralCode.code) == lower)
        .one_or_none()
    )


def count_owner_codes(db: Session, owner_id: int) -> int:
    return (
        db.query(func.count(ReferralCode.id))
        .filter(ReferralCode.owner_id == owner_id)
        .scalar()
        or 0
    )


def count_registrations(db: Session, code_id: int) -> int:
    return (
        db.query(func.count(ReferralCodeRegistration.id))
        .filter(ReferralCodeRegistration.code_id == code_id)
        .scalar()
        or 0
    )


def count_non_reversed_usages(db: Session, code_id: int, referee_id: int) -> int:
    """Active (non-reversed) usages for the (code, referee) pair. Used by
    the 5-per-referee cap check. Refund flips reversed_at on the row, so
    a refunded payment frees the slot — symmetric with the spec.
    """
    return (
        db.query(func.count(ReferralCodeUsage.id))
        .filter(
            ReferralCodeUsage.code_id == code_id,
            ReferralCodeUsage.referee_id == referee_id,
            ReferralCodeUsage.reversed_at.is_(None),
        )
        .scalar()
        or 0
    )


# ── Code generation ═════════════════════════════════════════════════════════

def generate_unique_code(db: Session, length: int = 7, max_tries: int = 12) -> str:
    """Suggested code generator. Used by future "auto-suggest" UX; not on
    the registration path. Retries on the LOWER(code) collision."""
    for _ in range(max_tries):
        candidate = "".join(secrets.choice(_GEN_ALPHABET) for _ in range(length))
        if find_code_by_string(db, candidate) is None:
            return candidate
    raise CodeServiceError("Could not generate a unique code; try again")


# ── Create paths (the security-critical pair) ═══════════════════════════════

@dataclass
class CodeCreatePayload:
    """Normalized create request — both endpoints construct one of these.
    Decimal precision matches the Numeric(5,2) column so 25.01 != 25.00
    is preserved end-to-end."""
    code: str
    commission_pct: Decimal
    discount_pct: Decimal


def _normalize_create_payload(
    code: str, commission_pct, discount_pct
) -> CodeCreatePayload:
    return CodeCreatePayload(
        code=_validate_code_format(code),
        commission_pct=_to_decimal(commission_pct),
        discount_pct=_to_decimal(discount_pct),
    )


def create_self_serve_code(
    db: Session, *, owner: User, code: str, commission_pct, discount_pct
) -> ReferralCode:
    """User-facing create. Pool capped at 25 — REJECTED before INSERT so
    the DB CHECK is the second line of defense, not the first error the
    user sees.

    Defense layers (defense-in-depth, per spec):
      Layer 1 — endpoint: distinct route from admin create.
      Layer 2 — this service: enforces SELF_SERVE_CAP_PCT.
      Layer 3 — DB CHECK: ck_referral_codes_high_pool_needs_admin.
    """
    payload = _normalize_create_payload(code, commission_pct, discount_pct)
    total = payload.commission_pct + payload.discount_pct
    if total > SELF_SERVE_CAP_PCT:
        raise PoolCapExceededError(
            f"Self-serve cap is {SELF_SERVE_CAP_PCT}%; requested {total}%"
        )
    if count_owner_codes(db, owner.id) >= CODES_PER_OWNER_MAX:
        raise OwnerCodeCapError(
            f"Already at owner cap ({CODES_PER_OWNER_MAX} codes)"
        )
    if find_code_by_string(db, payload.code) is not None:
        raise CodeTakenError("Code already taken (case-insensitive)")
    row = ReferralCode(
        owner_id=owner.id,
        code=payload.code,
        commission_pct=payload.commission_pct,
        discount_pct=payload.discount_pct,
        code_type="self_serve",
        created_by_admin_id=None,
    )
    db.add(row)
    db.flush()
    return row


def create_admin_code(
    db: Session,
    *,
    admin: User,
    owner_id: Optional[int],
    code: str,
    commission_pct,
    discount_pct,
) -> ReferralCode:
    """Admin-facing create. Pool capped at 45 (the global cap).

    Caller MUST have verified admin role at the route layer
    (Depends(get_admin_user)) — this function trusts `admin` blindly.
    """
    payload = _normalize_create_payload(code, commission_pct, discount_pct)
    total = payload.commission_pct + payload.discount_pct
    if total > GLOBAL_CAP_PCT:
        raise PoolCapExceededError(
            f"Admin cap is {GLOBAL_CAP_PCT}%; requested {total}%"
        )
    # Resolve owner — defaults to admin themselves.
    effective_owner_id = owner_id if owner_id is not None else admin.id
    if owner_id is not None:
        target = db.query(User).filter(User.id == owner_id).one_or_none()
        if target is None:
            raise CodeServiceError(f"owner_id {owner_id} does not exist")
    if count_owner_codes(db, effective_owner_id) >= CODES_PER_OWNER_MAX:
        raise OwnerCodeCapError(
            f"Owner #{effective_owner_id} is at code cap ({CODES_PER_OWNER_MAX})"
        )
    if find_code_by_string(db, payload.code) is not None:
        raise CodeTakenError("Code already taken (case-insensitive)")
    row = ReferralCode(
        owner_id=effective_owner_id,
        code=payload.code,
        commission_pct=payload.commission_pct,
        discount_pct=payload.discount_pct,
        code_type="admin",
        created_by_admin_id=admin.id,
    )
    db.add(row)
    db.flush()
    return row


# ── Registration (binds referee to code at signup) ══════════════════════════

def bind_referee(db: Session, *, referee: User, raw_code: str) -> Optional[ReferralCode]:
    """Idempotent-friendly: if `raw_code` is empty/None, returns None
    silently (user registered without a code). If the code doesn't
    exist, also returns None — the registration succeeds without a
    referral binding. This matches the spec "Если код невалиден →
    silent skip".

    Cases that DO raise (visible to the registration endpoint as a
    user-facing error):
      * SelfReferralError — referee.username == code.owner.username
      * RegistrationCapError — code already has 15 referees
      * already-bound — DB UNIQUE(referee_id) will fire if a second
        bind is attempted; raised here pre-flight for a clearer error
    """
    code = find_code_by_string(db, raw_code) if raw_code else None
    if code is None:
        return None
    if code.owner_id == referee.id:
        raise SelfReferralError("Cannot register under your own code")
    if count_registrations(db, code.id) >= REGISTRATIONS_CAP:
        raise RegistrationCapError(
            f"Code is closed for new signups (cap {REGISTRATIONS_CAP})"
        )
    # Pre-flight: was this referee already bound (e.g. retry after a
    # partial registration failure)? The DB UNIQUE(referee_id) would
    # catch it, but raising here is clearer.
    existing = (
        db.query(ReferralCodeRegistration)
        .filter(ReferralCodeRegistration.referee_id == referee.id)
        .one_or_none()
    )
    if existing is not None:
        raise CodeServiceError("Referee already bound to a code")

    db.add(ReferralCodeRegistration(code_id=code.id, referee_id=referee.id))
    referee.signup_code_id = code.id
    db.flush()
    return code


# ── Serialization ════════════════════════════════════════════════════════════
# Two views — owner sees commission, public preview does NOT.

def serialize_code_for_owner(code: ReferralCode, *, db: Session) -> dict:
    """Full view, returned to the owner (and admin)."""
    return {
        "id": code.id,
        "code": code.code,
        "code_type": code.code_type,
        "commission_pct": float(code.commission_pct),
        "discount_pct": float(code.discount_pct),
        "created_at": code.created_at.isoformat() if code.created_at else None,
        "created_by_admin_id": code.created_by_admin_id,
        "registrations_used": count_registrations(db, code.id),
        "registrations_remaining": max(0, REGISTRATIONS_CAP - count_registrations(db, code.id)),
    }


def serialize_code_for_preview(code: ReferralCode, *, db: Session) -> dict:
    """Public preview — discount only. commission_pct withheld (private
    to the owner; another user shouldn't be able to enumerate owners'
    cuts via this endpoint).
    """
    used = count_registrations(db, code.id)
    return {
        "code": code.code,
        "discount_pct": float(code.discount_pct),
        "registrations_remaining": max(0, REGISTRATIONS_CAP - used),
        "is_open": used < REGISTRATIONS_CAP,
    }


# ── Pricing + accrual (consumed by checkout + webhook) ══════════════════════

def effective_discount_pct(code: Optional[ReferralCode], usage_count: int) -> Decimal:
    """Returns the discount % that actually applies to a checkout.
    Returns 0 when:
      - no code bound
      - the (code, referee) pair has hit USAGES_PER_REFEREE
      - the code somehow has discount_pct > GLOBAL_CAP_PCT (defensive
        clamp; CHECK should already prevent this).
    """
    if code is None or usage_count >= USAGES_PER_REFEREE:
        return Decimal("0")
    raw = Decimal(code.discount_pct)
    if raw > GLOBAL_CAP_PCT:
        return GLOBAL_CAP_PCT
    if raw < 0:
        return Decimal("0")
    return raw


def effective_commission_pct(code: Optional[ReferralCode], usage_count: int) -> Decimal:
    """Symmetric to effective_discount_pct. Used by payment_service at
    webhook to compute the commission_earned for the Usage row + the
    pcb-Earning row."""
    if code is None or usage_count >= USAGES_PER_REFEREE:
        return Decimal("0")
    raw = Decimal(code.commission_pct)
    if raw > GLOBAL_CAP_PCT:
        return GLOBAL_CAP_PCT
    if raw < 0:
        return Decimal("0")
    return raw
