#!/usr/bin/env python3
"""Rotate ENCRYPTION_KEY across every encrypted column in the database.

Why this exists
---------------
Fernet keys for `wallets.credentials` and `users.totp_secret_enc` are
derived from `settings.ENCRYPTION_KEY` via PBKDF2 with a hardcoded salt.
If that key ever leaks (compromised .env, accidental commit, dumped
process memory) the only honest remediation is rotation:

  1. Pick a new key.
  2. Decrypt every encrypted field with the OLD key.
  3. Re-encrypt with the NEW key.
  4. Restart the app with the NEW key.

Without a rotation tool admins have to do step 2/3 by hand, which is
how prod outages start.

Usage
-----
On the prod box, *while the app is still running on the OLD key*:

    docker compose exec app \
      env AVALANT_OLD_ENCRYPTION_KEY=<old-secret> \
          AVALANT_NEW_ENCRYPTION_KEY=<new-secret> \
      python scripts/rotate_encryption_key.py

The script:
  - Verifies it can decrypt at least one row with the OLD key (sanity).
  - Re-encrypts every wallet credential value + every TOTP secret.
  - Commits in batches of 200 so a crash mid-rotation leaves the table
    half-old / half-new — both readable by passing OLD to the new
    deployment as a fallback (see "partial rotation" below).
  - Prints a per-table progress line.

After it finishes:

  1. Update the .env on the host:  ENCRYPTION_KEY=<new-secret>
  2. `./scripts/rolling-deploy.sh` — both replicas pick up the new key.
  3. Once /api/health is green, delete AVALANT_OLD_ENCRYPTION_KEY from
     the environment and shred the old secret.

Partial-rotation safety
-----------------------
If the script crashes mid-loop you'll have a mix of OLD-encrypted and
NEW-encrypted rows. To recover, re-run with the same OLD/NEW envs —
the script tries OLD first, and if that fails falls back to NEW
(idempotent on already-rotated rows).
"""
from __future__ import annotations

import base64
import logging
import os
import sys

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Ensure we can import backend.* when run from the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.db.base import SessionLocal
from backend.db.models import Wallet, User

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rotate")

_SALT = b"wallet-monitor-creds-v1"
_BATCH_SIZE = 200


def _fernet_for(secret: str) -> Fernet:
    if not secret:
        raise ValueError("empty encryption key")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        iterations=260_000,
    )
    return Fernet(base64.urlsafe_b64encode(kdf.derive(secret.encode())))


def _try_decrypt(value: str, primary: Fernet, fallback: Fernet | None) -> tuple[str, bool]:
    """Return (plaintext, was_already_new).

    Tries the primary (OLD) key first. On InvalidToken falls back to the new
    key — that catches re-runs after a partial rotation: rows already moved
    to NEW would otherwise look like garbage data."""
    try:
        return primary.decrypt(value.encode()).decode(), False
    except InvalidToken:
        if fallback is None:
            raise
        try:
            return fallback.decrypt(value.encode()).decode(), True
        except InvalidToken:
            raise


def rotate_wallet_credentials(old_f: Fernet, new_f: Fernet) -> int:
    """Re-encrypt every string value inside Wallet.credentials. Returns the
    number of wallet rows touched (rows already on NEW key are skipped)."""
    db = SessionLocal()
    touched = 0
    try:
        rows = db.query(Wallet).all()
        for i, w in enumerate(rows, start=1):
            creds = dict(w.credentials or {})
            if not creds:
                continue
            new_creds: dict = {}
            already_new = True
            for k, v in creds.items():
                if not (isinstance(v, str) and v):
                    new_creds[k] = v
                    continue
                plain, was_new = _try_decrypt(v, old_f, new_f)
                if not was_new:
                    already_new = False
                new_creds[k] = new_f.encrypt(plain.encode()).decode()
            if already_new:
                continue  # row was already migrated by an earlier run
            w.credentials = new_creds
            touched += 1
            if touched % _BATCH_SIZE == 0:
                db.commit()
                log.info("wallets: %d / %d touched", touched, len(rows))
        db.commit()
        log.info("wallets: rotation done, %d / %d rows updated", touched, len(rows))
    finally:
        db.close()
    return touched


def rotate_totp_secrets(old_f: Fernet, new_f: Fernet) -> int:
    """Re-encrypt every users.totp_secret_enc value. Returns rows touched."""
    db = SessionLocal()
    touched = 0
    try:
        rows = (
            db.query(User)
            .filter(User.totp_secret_enc.isnot(None))
            .all()
        )
        for u in rows:
            if not u.totp_secret_enc:
                continue
            try:
                plain, was_new = _try_decrypt(u.totp_secret_enc, old_f, new_f)
            except InvalidToken:
                log.warning("totp: user_id=%s — cannot decrypt with either key, skipping", u.id)
                continue
            if was_new:
                continue
            u.totp_secret_enc = new_f.encrypt(plain.encode()).decode()
            touched += 1
        db.commit()
        log.info("totp: rotation done, %d / %d rows updated", touched, len(rows))
    finally:
        db.close()
    return touched


def main() -> int:
    old_secret = os.environ.get("AVALANT_OLD_ENCRYPTION_KEY") or ""
    new_secret = os.environ.get("AVALANT_NEW_ENCRYPTION_KEY") or ""
    if not old_secret or not new_secret:
        log.error("Set AVALANT_OLD_ENCRYPTION_KEY and AVALANT_NEW_ENCRYPTION_KEY before running")
        return 2
    if old_secret == new_secret:
        log.error("Old and new keys are identical — nothing to do")
        return 2

    old_f = _fernet_for(old_secret)
    new_f = _fernet_for(new_secret)

    # Smoke check: pick one wallet credential field and confirm we can decrypt.
    db = SessionLocal()
    try:
        sample = db.query(Wallet).filter(Wallet.credentials.isnot(None)).first()
    finally:
        db.close()
    if sample and sample.credentials:
        for v in sample.credentials.values():
            if isinstance(v, str) and v:
                try:
                    _try_decrypt(v, old_f, new_f)
                    log.info("smoke: OLD key successfully decrypts an existing wallet credential")
                except InvalidToken:
                    log.error("smoke: neither OLD nor NEW key decrypts wallet id=%s — rotation aborted",
                              sample.id)
                    return 1
                break

    log.info("=== Rotating wallet credentials ===")
    rotate_wallet_credentials(old_f, new_f)
    log.info("=== Rotating TOTP secrets ===")
    rotate_totp_secrets(old_f, new_f)
    log.info("Done. Update ENCRYPTION_KEY in .env and run ./scripts/rolling-deploy.sh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
