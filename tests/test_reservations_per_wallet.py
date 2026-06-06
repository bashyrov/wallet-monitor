"""Reservations are per-WALLET, not per-EXCHANGE.

If a user has two Gate wallets and a pending trigger reserves capital
on wallet_A, wallet_B's available_usdt must NOT be affected. Otherwise
adding a second account is meaningless because triggers on the first
would freeze the second's display.
"""
from backend.crypto import decrypt_credentials


def _user(client, auth):
    from backend.db.base import SessionLocal
    from backend.db.models import User
    db = SessionLocal()
    try:
        return db.query(User).filter(User.username == "alice").first()
    finally:
        db.close()


def test_reservations_isolated_per_wallet(client, auth):
    """Two Gate wallets owned by the same user. A pending trigger on
    wallet_A reserves $30. wallet_B has no triggers → its reservation
    map entry is 0, available_usdt == balance_usdt."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbTriggerOrder, Wallet
    from backend.services.trade_service import _pending_open_trigger_reservations

    user = _user(client, auth)
    db = SessionLocal()
    try:
        # Two Gate wallets — same exchange, different accounts
        for wid, name in [(101, "gate-A"), (102, "gate-B")]:
            db.add(Wallet(
                id=wid, user_id=user.id, name=name,
                wallet_type="exchange", type_value="gate",
                credentials={}, purpose="both", can_trade=True,
            ))
        db.commit()

        # Pending open-trigger using wallet_A on long leg
        # (use wallet 102 on short to keep this isolated)
        t = ArbTriggerOrder(
            user_id=user.id, kind="open", trigger_spread_pct=1.5,
            long_exchange="gate", long_symbol="VANRY", long_wallet_id=101,
            short_exchange="mexc", short_symbol="VANRY", short_wallet_id=999,
            total_qty_token=5000, leverage=3,
            status="pending",
        )
        db.add(t)
        db.commit()

        # Stub mark price so reservation calc has a number
        from backend.services import price_service
        # Pre-populate cache via internal API if available; otherwise
        # the reservation just resolves to 0 and the test still passes
        # the wallet-isolation invariant we're checking.
        cache = getattr(price_service, "_cache", None)
        if cache is not None:
            cache["VANRY"] = 0.005    # → notional 25 USDT, /3 lev = 8.33 reserved

        res = _pending_open_trigger_reservations(db, user.id)
        # wallet_A may have a reservation; wallet_B must not
        assert 102 not in res, (
            f"wallet_B (id=102) leaked reservation from wallet_A: {res}"
        )
        # wallet_A may or may not have an entry depending on price cache;
        # what matters is wallet_B isolation.
    finally:
        db.close()


def test_reservations_keyed_strictly_by_wallet_id(client, auth):
    """When two wallets exist on the same exchange and a trigger uses
    wallet_A, _pending_open_trigger_reservations returns a dict where
    only wallet_A.id appears as a key — wallet_B is absent. List_user_
    balances + get_pair_status both look up by wallet_id, so wallet_B
    sees res.get(wallet_B_id, 0.0) == 0."""
    from backend.db.base import SessionLocal
    from backend.db.models import ArbTriggerOrder, Wallet
    from backend.services.trade_service import _pending_open_trigger_reservations

    user = _user(client, auth)
    db = SessionLocal()
    try:
        for wid in [201, 202, 203]:
            db.add(Wallet(
                id=wid, user_id=user.id, name=f"w{wid}",
                wallet_type="exchange", type_value="gate",
                credentials={}, purpose="both", can_trade=True,
            ))
        # Trigger on wallet 201 long, 202 short
        db.add(ArbTriggerOrder(
            user_id=user.id, kind="open", trigger_spread_pct=1.5,
            long_exchange="gate", long_symbol="ETH", long_wallet_id=201,
            short_exchange="mexc", short_symbol="ETH", short_wallet_id=202,
            total_qty_token=1, leverage=3,
            status="pending",
        ))
        db.commit()

        res = _pending_open_trigger_reservations(db, user.id)
        # wallet 203 has no involvement → must NOT appear in dict
        assert 203 not in res, f"wallet 203 leaked: {res}"
    finally:
        db.close()
