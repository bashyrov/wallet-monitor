"""TOTP helpers — Fernet-encrypt the secret at rest, generate the
provisioning URI for the QR code, and verify codes with a small ±1
step skew tolerance for clock drift.

Open to all users (not just admins): /me/2fa/* endpoints in auth.py.
Login flow issues a totp_challenge token whenever the user has
totp_verified_at set.

Recovery codes: 8 single-use codes generated at verify-time, stored as
bcrypt hashes in users.totp_recovery_codes. Spend one to log in if the
authenticator app is lost.
"""
from __future__ import annotations

import base64
import logging
import os
import secrets
from datetime import datetime
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from passlib.hash import bcrypt

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


def qr_data_uri(otpauth_uri: str, *, scale: int = 1) -> str:
    """Render the otpauth URI as an SVG QR and return a data: URI ready
    for an <img src="..."> tag. Avoids any client-side library / CDN
    dependency. Server-side render is ~3 ms / ~1 KB output."""
    import io
    import base64
    import segno
    qr = segno.make(otpauth_uri, error="m")
    buf = io.BytesIO()
    # SVG output — vector, scales cleanly to any container size client-side.
    # `xmldecl=False` strips the <?xml ...?> header so the data URI stays
    # short and renders inline reliably across browsers.
    qr.save(buf, kind="svg", scale=scale, border=2, dark="#0B0B0E", light="#FFFFFF",
            xmldecl=False, svgns=True, omitsize=False)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


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


def generate_recovery_codes(n: int = 8) -> list[str]:
    """Return n plaintext recovery codes (format `xxxx-xxxx`)."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/O, 1/l/I for legibility
    out: list[str] = []
    for _ in range(n):
        chunk1 = "".join(secrets.choice(alphabet) for _ in range(4))
        chunk2 = "".join(secrets.choice(alphabet) for _ in range(4))
        out.append(f"{chunk1}-{chunk2}")
    return out


def hash_recovery_codes(codes: list[str]) -> list[str]:
    return [bcrypt.hash(c) for c in codes]


def verify_and_consume_recovery_code(stored_hashes: list[str], code: str) -> tuple[bool, list[str]]:
    """Try to match `code` against `stored_hashes`. On match: return
    (True, hashes-with-matched-entry-removed). On miss: (False, stored_hashes)."""
    if not code or not stored_hashes:
        return False, stored_hashes
    candidate = code.strip().lower()
    for i, h in enumerate(stored_hashes):
        try:
            if bcrypt.verify(candidate, h):
                return True, stored_hashes[:i] + stored_hashes[i+1:]
        except Exception:
            continue
    return False, stored_hashes


def is_recovery_format(code: str) -> bool:
    """Heuristic: TOTP codes are 6 digits; recovery codes are 9 chars (4-4 with hyphen)."""
    s = (code or "").strip()
    return "-" in s and len(s) >= 8
