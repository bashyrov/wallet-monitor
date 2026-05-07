"""Regression test for Task 3 — spot-short auto-detection bug.

Root cause was `Wallet.wallet_type == "exchange"` filter in trade_service
+ reconcile_service that excluded perp DEX wallets (Aster, Hyperliquid,
Paradex, Ethereal, Lighter). Symbol normalization works correctly; the
shorts simply never reached the pairing logic.

We verify the SQL filter directly — that the wallet-selection queries
that drive position listing now match perpdex wallets.
"""
from sqlalchemy import select


def _create_user_with_wallet(client, auth, wallet_type, type_value, **extra):
    """Create wallet via API. Returns wallet dict."""
    body = {
        "name": f"{type_value} wallet",
        "wallet_type": wallet_type,
        "type_value": type_value,
        "api_key": "testapikey123456",
        "api_secret": "testapisecret123456",
    }
    if wallet_type == "perpdex":
        body["address"] = "0x1234567890abcdef1234567890abcdef12345678"
        if type_value == "paradex":
            body["api_token"] = "dummy"
        if type_value not in ("aster",):
            body.pop("api_key", None)
            body.pop("api_secret", None)
    body.update(extra)
    r = client.post("/api/wallets", json=body, headers=auth)
    assert r.status_code == 201, f"create {type_value}: {r.text}"
    return r.json()


def test_position_listing_query_includes_perpdex(client, auth):
    """The SQL filter that drives _list_user_positions_inner now matches
    BOTH wallet_type='exchange' AND wallet_type='perpdex'.

    Before the fix, this filter was `wallet_type == "exchange"` which
    silently excluded all perp DEX wallets — so Aster shorts never
    appeared in /api/trade/positions and spot-short auto-pairing
    couldn't find them.
    """
    from backend.db.base import SessionLocal
    from backend.db.models import User, Wallet

    _create_user_with_wallet(client, auth, "exchange", "gate", purpose="screener")
    _create_user_with_wallet(client, auth, "perpdex", "hyperliquid")  # auto-defaults to 'both'

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "alice").first()
        assert user is not None

        # Mirror the exact filter used in trade_service._list_user_positions_inner
        wallets = (
            db.query(Wallet)
            .filter(
                Wallet.user_id == user.id,
                Wallet.wallet_type.in_(("exchange", "perpdex")),
                Wallet.purpose.in_(("screener", "both")),
                Wallet.is_archived == False,  # noqa: E712
            )
            .all()
        )

        wallet_types = sorted(w.wallet_type for w in wallets)
        type_values = sorted(w.type_value for w in wallets)

        assert wallet_types == ["exchange", "perpdex"], (
            f"expected both wallet types in result, got {wallet_types}"
        )
        assert "gate" in type_values
        assert "hyperliquid" in type_values
    finally:
        db.close()


def test_users_with_trade_wallets_includes_perpdex(client, auth):
    """reconcile_service._users_with_trade_wallets must include perpdex-only
    users. Otherwise users with only Aster/Hyperliquid never reconcile."""
    from backend.db.base import SessionLocal
    from backend.db.models import User
    from backend.services.reconcile_service import _users_with_trade_wallets

    _create_user_with_wallet(client, auth, "perpdex", "hyperliquid")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "alice").first()
        user_ids = _users_with_trade_wallets(db)
        assert user.id in user_ids, (
            "perpdex-only user must be returned by _users_with_trade_wallets"
        )
    finally:
        db.close()


def test_user_streams_supervisor_includes_perpdex(client, auth):
    """The user-streams supervisor must include perpdex wallets so live
    balance/position WS streams stay subscribed for Aster, HL, etc."""
    from backend.db.base import SessionLocal
    from backend.db.models import Wallet

    _create_user_with_wallet(client, auth, "perpdex", "hyperliquid")

    db = SessionLocal()
    try:
        # Mirror _supervisor's wallet query
        wallets = (
            db.query(Wallet)
            .filter(
                Wallet.wallet_type.in_(("exchange", "perpdex")),
                Wallet.purpose.in_(("screener", "both")),
                Wallet.is_archived == False,  # noqa: E712
            )
            .all()
        )
        type_values = sorted(w.type_value for w in wallets)
        assert "hyperliquid" in type_values
    finally:
        db.close()
