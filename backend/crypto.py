"""Fernet-based encryption for wallet credentials."""
import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from settings import settings

_SALT = b"wallet-monitor-creds-v1"


def _fernet() -> Fernet:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        iterations=260_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(settings.ENCRYPTION_KEY.encode()))
    return Fernet(key)


def encrypt_value(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, Exception):
        # Not encrypted (legacy plain-text) — return as-is
        return value


def encrypt_credentials(creds: dict) -> dict:
    """Encrypt all string values in a credentials dict."""
    return {
        k: encrypt_value(v) if isinstance(v, str) and v else v
        for k, v in creds.items()
    }


def decrypt_credentials(creds: dict) -> dict:
    """Decrypt all string values in a credentials dict."""
    return {
        k: decrypt_value(v) if isinstance(v, str) and v else v
        for k, v in creds.items()
    }
