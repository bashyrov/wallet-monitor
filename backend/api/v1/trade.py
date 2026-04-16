"""Live trading endpoints — real order placement on supported exchanges."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.deps import get_current_user, get_db
from backend.db.models import User, Wallet
from backend.services import trade_service
from backend.services.trade_adapters import SUPPORTED_EXCHANGES

router = APIRouter(prefix="/trade", tags=["trade"])
logger = logging.getLogger("avalant.trade")


# ── Read ──────────────────────────────────────────────────────────────────────
@router.get("/status")
async def pair_status(
    symbol: str = Query(...),
    long_ex: str = Query(...),
    short_ex: str = Query(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await trade_service.get_pair_status(db, user.id, symbol, long_ex.lower(), short_ex.lower())


@router.get("/positions")
async def positions(
    symbol: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await trade_service.list_user_positions(db, user.id, symbol)


@router.get("/supported")
def supported_exchanges():
    return sorted(SUPPORTED_EXCHANGES)


# ── Write ─────────────────────────────────────────────────────────────────────
class OpenOrderIn(BaseModel):
    wallet_id: int
    symbol: str
    side: str = Field(..., pattern="^(buy|sell)$")
    quantity: float = Field(..., gt=0)
    leverage: int = Field(3, ge=1, le=125)
    margin_mode: str = Field("isolated", pattern="^(isolated|cross)$")


@router.post("/open")
async def open_order(
    body: OpenOrderIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return await trade_service.place_open_order(
            db, user.id, body.wallet_id, body.symbol, body.side, body.quantity,
            body.leverage, body.margin_mode,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.warning("open order failed uid=%s wid=%s: %s", user.id, body.wallet_id, e)
        raise HTTPException(502, f"Exchange rejected order: {e}")


class CloseIn(BaseModel):
    wallet_id: int
    symbol: str
    side: str | None = None


@router.post("/close")
async def close_order(
    body: CloseIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return await trade_service.close_position(db, user.id, body.wallet_id, body.symbol, body.side)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.warning("close failed uid=%s wid=%s: %s", user.id, body.wallet_id, e)
        raise HTTPException(502, f"Exchange rejected close: {e}")


# ── Enable/disable trading on a wallet ────────────────────────────────────────
class ToggleIn(BaseModel):
    can_trade: bool


@router.patch("/wallets/{wallet_id}")
def toggle_can_trade(
    wallet_id: int,
    body: ToggleIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    w = db.query(Wallet).filter(Wallet.id == wallet_id, Wallet.user_id == user.id).first()
    if not w:
        raise HTTPException(404, "Wallet not found")
    if w.wallet_type != "exchange":
        raise HTTPException(400, "Trading can only be enabled on exchange wallets")
    if body.can_trade and w.type_value not in SUPPORTED_EXCHANGES:
        raise HTTPException(400, f"{w.type_value} trading is not supported yet")
    w.can_trade = body.can_trade
    db.commit()
    return {"id": w.id, "can_trade": w.can_trade}
