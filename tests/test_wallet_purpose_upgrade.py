"""PATCH /api/wallets/{id} upgrade flow:
user creates a portfolio-only perpdex wallet (no private key), later
edits the wallet to add the private key AND flip purpose to screener
in the same request.
"""
from backend.crypto import decrypt_credentials


def test_upgrade_hl_portfolio_to_screener_with_private_key(client, auth):
    """User originally added an HL wallet read-only (just address). Now
    wants to enable trading — provides private_key + flips purpose=both
    in one PATCH. Backend stores the key and updates purpose."""
    r = client.post("/api/wallets", json={
        "name": "HL read-only first",
        "wallet_type": "perpdex",
        "type_value": "hyperliquid",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "purpose": "portfolio",
    }, headers=auth)
    assert r.status_code == 201, r.text
    wid = r.json()["id"]

    # PATCH to add private_key + flip purpose
    r = client.patch(f"/api/wallets/{wid}", json={
        "private_key": "0x" + "a" * 64,
        "purpose": "both",
    }, headers=auth)
    assert r.status_code == 200, r.text

    # Verify both stored — key is under api_secret, purpose flipped
    from backend.db.base import SessionLocal
    from backend.db.models import Wallet
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.id == wid).first()
        assert w.purpose == "both"
        creds = decrypt_credentials(w.credentials)
        assert creds.get("api_secret") == "0x" + "a" * 64
        # Address stays put
        assert creds.get("address") == "0x1234567890abcdef1234567890abcdef12345678"
    finally:
        db.close()


def test_upgrade_paradex_adds_l2_key(client, auth, monkeypatch):
    """Paradex: user has portfolio wallet with just JWT, adds l2_private_key
    via PATCH and flips to screener.

    Paradex Python adapter is read-only (paradex-py SDK incompatible with
    Python 3.13); trading routes through Go-fetcher when GO_TRADE_VENUES
    includes it. Without that env, upgrade rejects — that's the prod-safe
    default. This test enables the Go path so the upgrade succeeds."""
    monkeypatch.setenv("GO_TRADE_VENUES", "paradex,binance")
    monkeypatch.setenv("AVALANT_INTERNAL_SECRET", "test-secret")

    r = client.post("/api/wallets", json={
        "name": "Paradex read-only",
        "wallet_type": "perpdex",
        "type_value": "paradex",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "api_token": "old-jwt",
        "purpose": "portfolio",
    }, headers=auth)
    wid = r.json()["id"]

    r = client.patch(f"/api/wallets/{wid}", json={
        "l2_private_key": "0x" + "b" * 64,
        "purpose": "both",
    }, headers=auth)
    assert r.status_code == 200, r.text

    from backend.db.base import SessionLocal
    from backend.db.models import Wallet
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.id == wid).first()
        assert w.purpose == "both"
        creds = decrypt_credentials(w.credentials)
        assert creds.get("private_key") == "0x" + "b" * 64
        assert creds.get("api_token") == "old-jwt"
    finally:
        db.close()


def test_paradex_upgrade_rejected_without_go_path(client, auth):
    """Without GO_TRADE_VENUES=paradex, Python adapter is read-only
    proxy → upgrade to trade-purpose is correctly rejected."""
    r = client.post("/api/wallets", json={
        "name": "Paradex no go",
        "wallet_type": "perpdex",
        "type_value": "paradex",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "api_token": "jwt",
        "purpose": "portfolio",
    }, headers=auth)
    wid = r.json()["id"]

    r = client.patch(f"/api/wallets/{wid}", json={
        "l2_private_key": "0x" + "c" * 64,
        "purpose": "both",
    }, headers=auth)
    assert r.status_code == 400
    assert "not supported yet" in r.json()["detail"]


def test_upgrade_lighter_adds_three_creds(client, auth):
    """Lighter needs account_index + private_key + api_key_index."""
    r = client.post("/api/wallets", json={
        "name": "Lighter read-only",
        "wallet_type": "perpdex",
        "type_value": "lighter",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "purpose": "portfolio",
    }, headers=auth)
    wid = r.json()["id"]

    r = client.patch(f"/api/wallets/{wid}", json={
        "account_index": "12345",
        "private_key": "0x" + "c" * 64,
        "api_key_index": "1",
        "purpose": "both",
    }, headers=auth)
    assert r.status_code == 200, r.text

    from backend.db.base import SessionLocal
    from backend.db.models import Wallet
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.id == wid).first()
        assert w.purpose == "both"
        creds = decrypt_credentials(w.credentials)
        assert creds.get("api_key") == "12345"
        assert creds.get("api_secret") == "0x" + "c" * 64
        assert creds.get("api_passphrase") == "1"
    finally:
        db.close()


def test_downgrade_back_to_portfolio_only(client, auth):
    """User can also flip back from screener/both → portfolio without
    deleting credentials (key just stops being used for trading)."""
    r = client.post("/api/wallets", json={
        "name": "HL trading first",
        "wallet_type": "perpdex",
        "type_value": "hyperliquid",
        "address": "0x1234567890abcdef1234567890abcdef12345678",
        "private_key": "0x" + "a" * 64,
        "purpose": "both",
    }, headers=auth)
    wid = r.json()["id"]

    r = client.patch(f"/api/wallets/{wid}", json={
        "purpose": "portfolio",
    }, headers=auth)
    assert r.status_code == 200, r.text

    from backend.db.base import SessionLocal
    from backend.db.models import Wallet
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.id == wid).first()
        assert w.purpose == "portfolio"
        # Key stays — user might re-upgrade later
        creds = decrypt_credentials(w.credentials)
        assert creds.get("api_secret") == "0x" + "a" * 64
    finally:
        db.close()


def test_upgrade_exchange_wallet_purpose(client, auth):
    """CEX wallet: user created portfolio-only, later wants screener."""
    r = client.post("/api/wallets", json={
        "name": "Gate read-only",
        "wallet_type": "exchange",
        "type_value": "gate",
        "api_key": "k" * 16,
        "api_secret": "s" * 16,
        "purpose": "portfolio",
    }, headers=auth)
    wid = r.json()["id"]

    r = client.patch(f"/api/wallets/{wid}", json={
        "purpose": "both",
    }, headers=auth)
    assert r.status_code == 200, r.text

    from backend.db.base import SessionLocal
    from backend.db.models import Wallet
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.id == wid).first()
        assert w.purpose == "both"
    finally:
        db.close()
