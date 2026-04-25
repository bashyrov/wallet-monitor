"""TOTP helpers — Fernet-encrypt the secret at rest, generate the
provisioning URI for the QR code, and verify codes with a small ±1
step skew tolerance for clock drift.

Used by /api/auth/admin-2fa/* endpoints. Non-admin users never go
through this code path — the migration leaves totp_* NULL for them
and the auth router checks `is_admin` before invoking the gate.
"""
from __future__ import annotations

import base64
import logging
import os
from datetime import datetime
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("avalant.totp")


def _fernet() -> Fernet:
    # Re-use the wallet-credentials encryption key. Fresh enrolment on
    # rotation is acceptable since 2FA secrets are easy to re-issue.
    from settings import settings
    src = (settings.ENCRYPTION_KEY or "").encode("utf-8") or b"avalant-default"
    # 32 bytes → urlsafe-b64. Pad / truncate so any user-supplied key
    # length normalises to a valid Fernet input.
    if len(src) < 32:
        src = src.ljust(32, b"=")
    return Fernet(base64.urlsafe_b64encode(src[:32]))


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("utf-8")).decode("utf-8")


def decrypt_secret(blob: str) -> str:
    try:
        return _fernet().decrypt(blob.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        logger.warning("totp secret decrypt failed: %s", exc)
        raise


def generate_secret() -> str:
    import pyotp
    return pyotp.random_base32()


def provisioning_uri(secret: str, *, account: str, issuer: str = "Avalant") -> str:
    import pyotp
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)


def verify_code(secret: str, code: str, *, valid_window: int = 1) -> bool:
    """Verify a 6-digit code with `valid_window` steps tolerance on
    each side (default ±30 s)."""
    if not code or not code.strip().isdigit():
        return False
    import pyotp
    try:
        return pyotp.TOTP(secret).verify(code.strip(), valid_window=valid_window)
    except Exception as exc:
        logger.debug("totp verify exception: %s", exc)
        return False
