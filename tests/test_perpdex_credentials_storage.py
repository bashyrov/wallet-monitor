"""Task 1 follow-up — wire perpdex private-key fields through to storage.

Verifies that the WalletCreate body fields (private_key, l2_private_key,
account_index, api_key_index) get stored in the encrypted credentials
under the names the trade adapters consume.
"""
from backend.crypto import decrypt_credentials


def _post(client, auth, body):
    return client.post("/api/wallets", json=body, headers=auth)


def test_hyperliquid_private_key_stored_as_api_secret(client, auth):
    """HL adapter reads creds.get('private_key') OR creds.get('api_secret').
    We store under api_secret so the same code path works as Aster's PK."""
    r = _post(client, auth, {
        "name": "HL trading wallet",
        "wallet_type": "perpdex",
        "type_value": "hyperliquid",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "private_key": "0x" + "a" * 64,
        "purpose": "both",
    })
    assert r.status_code == 201, r.text
    wid = r.json()["id"]

    from backend.db.base import SessionLocal
    from backend.db.models import Wallet
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.id == wid).first()
        creds = decrypt_credentials(w.credentials)
        assert creds.get("address") == "0x1234567890abcdef1234567890abcdef12345678"
        assert creds.get("api_secret") == "0x" + "a" * 64
    finally:
        db.close()


def test_lighter_creds_three_field_mapping(client, auth):
    """Lighter: account_index → api_key, private_key → api_secret,
    api_key_index → api_passphrase. Default '255' filled in if blank."""
    r = _post(client, auth, {
        "name": "Lighter trading wallet",
        "wallet_type": "perpdex",
        "type_value": "lighter",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "account_index": "12345",
        "private_key": "0x" + "b" * 64,
        # Leave api_key_index blank — should default to "255"
        "purpose": "both",
    })
    assert r.status_code == 201, r.text
    wid = r.json()["id"]

    from backend.db.base import SessionLocal
    from backend.db.models import Wallet
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.id == wid).first()
        creds = decrypt_credentials(w.credentials)
        assert creds.get("api_key") == "12345"
        assert creds.get("api_secret") == "0x" + "b" * 64
        assert creds.get("api_passphrase") == "255"
    finally:
        db.close()


def test_lighter_explicit_api_key_index_overrides_default(client, auth):
    r = _post(client, auth, {
        "name": "Lighter custom key index",
        "wallet_type": "perpdex",
        "type_value": "lighter",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "account_index": "99",
        "private_key": "0x" + "c" * 64,
        "api_key_index": "1",
        "purpose": "both",
    })
    assert r.status_code == 201, r.text
    wid = r.json()["id"]

    from backend.db.base import SessionLocal
    from backend.db.models import Wallet
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.id == wid).first()
        creds = decrypt_credentials(w.credentials)
        assert creds.get("api_passphrase") == "1"
    finally:
        db.close()


def test_paradex_l2_private_key_stored_as_private_key(client, auth):
    """Paradex needs both api_token (JWT auth) and private_key (L2 stark
    key for SNIP-12 order signing). The L2 key comes in as l2_private_key."""
    r = _post(client, auth, {
        "name": "Paradex trading wallet",
        "wallet_type": "perpdex",
        "type_value": "paradex",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "api_token": "dummy-jwt",
        "l2_private_key": "0x" + "d" * 64,
        "purpose": "both",
    })
    assert r.status_code == 201, r.text
    wid = r.json()["id"]

    from backend.db.base import SessionLocal
    from backend.db.models import Wallet
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.id == wid).first()
        creds = decrypt_credentials(w.credentials)
        assert creds.get("api_token") == "dummy-jwt"
        assert creds.get("private_key") == "0x" + "d" * 64
        assert creds.get("address") == "0x1234567890abcdef1234567890abcdef12345678"
    finally:
        db.close()


def test_perpdex_trade_purpose_requires_keys(client, auth):
    """Creating a trade-purpose perpdex wallet without the right private-key
    fields fails fast with 422 — better than silent sign failures later."""
    # HL without private_key
    r = _post(client, auth, {
        "name": "HL no key",
        "wallet_type": "perpdex",
        "type_value": "hyperliquid",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "purpose": "screener",
    })
    assert r.status_code == 422
    assert "private_key" in r.json()["detail"]

    # Lighter without account_index
    r = _post(client, auth, {
        "name": "Lighter no idx",
        "wallet_type": "perpdex",
        "type_value": "lighter",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "private_key": "0x" + "a" * 64,
        "purpose": "screener",
    })
    assert r.status_code == 422
    assert "account_index" in r.json()["detail"]

    # Paradex without l2_private_key
    r = _post(client, auth, {
        "name": "Paradex no l2",
        "wallet_type": "perpdex",
        "type_value": "paradex",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "api_token": "jwt",
        "purpose": "screener",
    })
    assert r.status_code == 422
    assert "l2_private_key" in r.json()["detail"]


def test_perpdex_portfolio_purpose_works_without_keys(client, auth):
    """Read-only purpose (portfolio) doesn't need private keys — DEX read APIs
    work with just the address. Existing prod kошельки stay unaffected."""
    r = _post(client, auth, {
        "name": "HL read-only",
        "wallet_type": "perpdex",
        "type_value": "hyperliquid",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "purpose": "portfolio",
    })
    assert r.status_code == 201, r.text


def test_patch_can_add_keys_to_existing_perpdex(client, auth):
    """User creates a portfolio-only HL wallet, later wants to trade on it.
    PATCH should accept private_key + flip purpose."""
    r = _post(client, auth, {
        "name": "HL upgrade",
        "wallet_type": "perpdex",
        "type_value": "hyperliquid",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "purpose": "portfolio",
    })
    assert r.status_code == 201, r.text
    wid = r.json()["id"]

    r = client.patch(f"/api/wallets/{wid}", json={
        "private_key": "0x" + "e" * 64,
        "purpose": "both",
    }, headers=auth)
    assert r.status_code == 200, r.text

    from backend.db.base import SessionLocal
    from backend.db.models import Wallet
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.id == wid).first()
        creds = decrypt_credentials(w.credentials)
        assert creds.get("api_secret") == "0x" + "e" * 64
        assert w.purpose == "both"
    finally:
        db.close()


def test_toggle_trade_rejects_perpdex_without_keys(client, auth):
    """If user toggles can_trade=true via /api/trade/wallets/{id} on a
    perpdex wallet that lacks the private-key creds, we 400 with a clear
    message rather than letting the first signed request fail."""
    r = _post(client, auth, {
        "name": "HL no key bare",
        "wallet_type": "perpdex",
        "type_value": "hyperliquid",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "purpose": "portfolio",
    })
    wid = r.json()["id"]
    r = client.patch(f"/api/trade/wallets/{wid}", json={"can_trade": True}, headers=auth)
    assert r.status_code == 400
    assert "private_key" in r.json()["detail"]
