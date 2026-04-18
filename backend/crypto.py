"""Fernet-based encryption for wallet credentials."""
import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from settings import settings

_SALT = b"wallet-monitor-creds-v1"

# Cache the derived Fernet instance — PBKDF2 with 260k iterations runs once per
# process instead of once per credential field. The key is deterministic for a
# given ENCRYPTION_KEY, so there is no security impact, only huge perf win:
# listing 10 wallets (3 creds each) drops from ~3s to <1ms.
_cached_fernet: Fernet | None = None


def _fernet() -> Fernet:
    global _cached_fernet
    if _cached_fernet is None:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_SALT,
            iterations=260_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(settings.ENCRYPTION_KEY.encode()))
        _cached_fernet = Fernet(key)
    return _cached_fernet


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
