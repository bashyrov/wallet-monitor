"""Safety guard: Python fallback refused for venues where Python
close_position has different market semantics than the requested
market_type. See _PYTHON_CLOSE_FALLBACK_UNSAFE in trade_service.py.

Concrete trap covered: backpack Python close_position is a spot-sell.
If Go close fails (network blip) and we silently fall back, we'd dump
the user's SPOT balance instead of closing their perp leg — wrong leg
on a spot/short arb pair.

Service-level test (no FastAPI fixture) so it runs without orjson.
"""
import asyncio
import os
import sys
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

# Force SQLite + skip the conftest fixtures that try to init the FastAPI
# app (which requires orjson). We're testing the service layer directly.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-32-chars-long-aaaa")


def _in_memory_db():
    """Bootstrap a fresh in-memory DB + return a session."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from backend.db.base import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _make_user(db):
    from backend.db.models import User
    u = User(
        username="alice",
        email="alice@test.local",
        hashed_password="not-real",
        plan="free",
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


def _make_wallet(db, user, venue):
    from backend.db.models import Wallet
    w = Wallet(
        user_id=user.id,
        wallet_type="exchange",
        type_value=venue,
        name=f"{venue}-test",
        credentials={"api_key": "k", "api_secret": "s"},
        purpose="both",
        is_archived=False,
    )
    db.add(w); db.commit(); db.refresh(w)
    return w


def test_safety_table_includes_backpack_futures():
    """Static contract — guards against accidental removal of the entry."""
    from backend.services.trade_service import _PYTHON_CLOSE_FALLBACK_UNSAFE
    assert ("backpack", "futures") in _PYTHON_CLOSE_FALLBACK_UNSAFE
    msg = _PYTHON_CLOSE_FALLBACK_UNSAFE[("backpack", "futures")]
    assert "spot" in msg.lower()
    assert "wrong leg" in msg.lower() or "leg" in msg.lower()


def test_backpack_futures_close_refuses_python_fallback():
    """When Go close fails transiently for backpack/futures, the dispatcher
    must NOT silently invoke Python close (which sells SPOT). It must
    raise TradeError(kind=user) instead, surfacing the safety reason."""
    from backend.services import trade_service, trade_proxy

    db = _in_memory_db()
    user = _make_user(db)
    w = _make_wallet(db, user, "backpack")

    transient_err = trade_proxy.GoTradeError(kind="transient", message="net blip")

    from backend.services import plan_service
    fake_limits = MagicMock(trade_delay_ms=0)
    with patch.object(plan_service, "effective_limits", return_value=fake_limits), \
         patch.object(trade_proxy, "is_enabled", return_value=True), \
         patch.object(trade_proxy, "close_position", new=AsyncMock(side_effect=transient_err)):
        from backend.services.trade_adapters import ADAPTERS
        backpack_adapter = ADAPTERS["backpack"]
        # Spy on the Python adapter — if our guard fails, this WOULD be
        # called and would sell spot.
        with patch.object(backpack_adapter, "close_position", new=AsyncMock()) as spy_close:
            with pytest.raises(trade_service.TradeError) as excinfo:
                asyncio.run(trade_service.close_position(
                    db, user.id, w.id, "SOL", side="sell",
                    market_type="futures",
                ))
            assert excinfo.value.kind == "user"
            assert "spot" in str(excinfo.value).lower()
            # Python adapter MUST NEVER be invoked.
            spy_close.assert_not_called()


def test_non_unsafe_venue_still_falls_back():
    """Sanity check: a venue NOT in the unsafe set still falls back to
    Python on transient Go failure. Binance Python close is reduceOnly
    perp — semantically same as Go close, safe to fall back."""
    from backend.services import trade_service, trade_proxy

    db = _in_memory_db()
    user = _make_user(db)
    w = _make_wallet(db, user, "binance")

    transient_err = trade_proxy.GoTradeError(kind="transient", message="timeout")

    from backend.services import plan_service
    fake_limits = MagicMock(trade_delay_ms=0)
    with patch.object(plan_service, "effective_limits", return_value=fake_limits), \
         patch.object(trade_proxy, "is_enabled", return_value=True), \
         patch.object(trade_proxy, "close_position", new=AsyncMock(side_effect=transient_err)):
        from backend.services.trade_adapters import ADAPTERS
        binance_adapter = ADAPTERS["binance"]
        py_result = {"order_id": "py-fb", "closed_qty": 0.001, "realized_pnl_usd": 0}
        with patch.object(binance_adapter, "close_position",
                          new=AsyncMock(return_value=py_result)) as spy_close:
            result = asyncio.run(trade_service.close_position(
                db, user.id, w.id, "BTC", side="sell",
                market_type="futures",
            ))
            assert result["order_id"] == "py-fb"
            spy_close.assert_called_once()
