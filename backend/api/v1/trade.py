"""Live trading endpoints — real order placement on supported exchanges."""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,16}$")


def _vsym(v):
    s = str(v or "").strip().upper()
    if not _SYMBOL_RE.match(s):
        raise ValueError("symbol must be 1-16 alphanumeric uppercase")
    return s

from backend.api.deps import get_current_user, get_db
from backend.db.models import User, Wallet
from backend.services import trade_service
from backend.services.trade_adapters import SUPPORTED_EXCHANGES

router = APIRouter(prefix="/trade", tags=["trade"])
logger = logging.getLogger("avalant.trade")


# ── Read ──────────────────────────────────────────────────────────────────────
@router.get("/status")
async def pair_status(
    symbol: str = Query(..., pattern=r"^[A-Za-z0-9]{1,16}$"),
    long_ex: str = Query(..., min_length=2, max_length=24),
    short_ex: str = Query(..., min_length=2, max_length=24),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await trade_service.get_pair_status(db, user.id, symbol.upper(), long_ex.lower(), short_ex.lower())


@router.get("/positions")
async def positions(
    symbol: str | None = Query(None, pattern=r"^[A-Za-z0-9]{1,16}$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await trade_service.list_user_positions(db, user.id, symbol.upper() if symbol else None)


@router.get("/supported")
def supported_exchanges():
    from backend.services.trade_adapters import TRADE_SUPPORTED
    return {
        "trade": sorted(TRADE_SUPPORTED),
        "read_only": sorted(SUPPORTED_EXCHANGES - TRADE_SUPPORTED),
    }


@router.get("/leverage-limits")
async def leverage_limits(
    symbol: str = Query(...),
    long_ex: str = Query(...),
    short_ex: str = Query(...),
    _: User = Depends(get_current_user),
):
    """Public-only max leverage per leg — no API key required. Used by the
    trading panel to cap the leverage stepper so users can't pick a value
    the exchange will reject."""
    import asyncio
    from backend.services.trade_adapters import ADAPTERS, SUPPORTED_EXCHANGES

    async def _probe(ex: str) -> int | None:
        if ex not in SUPPORTED_EXCHANGES:
            return None
        adapter = ADAPTERS[ex]
        if not hasattr(adapter, "get_public_max_leverage"):
            return None
        try:
            return await adapter.get_public_max_leverage(symbol)
        except Exception:
            return None

    long_max, short_max = await asyncio.gather(
        _probe(long_ex.lower()), _probe(short_ex.lower()),
    )
    return {
        "symbol": symbol,
        "long":  {"exchange": long_ex.lower(),  "max_leverage": long_max},
        "short": {"exchange": short_ex.lower(), "max_leverage": short_max},
    }


class OpenArbIn(BaseModel):
    symbol: str
    long_wallet_id: int
    long_quantity: float = Field(..., gt=0, le=1_000_000)
    long_leverage: int = Field(3, ge=1, le=125)
    long_margin_mode: str = Field("isolated", pattern="^(isolated|cross)$")
    short_wallet_id: int
    short_quantity: float = Field(..., gt=0, le=1_000_000)
    short_leverage: int = Field(3, ge=1, le=125)
    short_margin_mode: str = Field("isolated", pattern="^(isolated|cross)$")

    @field_validator("symbol", mode="before")
    @classmethod
    def _v(cls, v): return _vsym(v)


@router.post("/open-arb")
async def open_arb(
    body: OpenArbIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fires both legs (long on long_wallet, short on short_wallet) in parallel.
    Returns per-leg success/error so the client can show exactly which side
    landed if one fails."""
    import asyncio
    if body.long_wallet_id == body.short_wallet_id:
        raise HTTPException(400, "long_wallet_id and short_wallet_id must differ")

    async def _one(leg: str, wid: int, qty: float, lev: int, mode: str, side: str):
        try:
            r = await trade_service.place_open_order(
                db, user.id, wid, body.symbol, side, qty, lev, mode,
            )
            return {"leg": leg, "ok": True, **r}
        except Exception as e:
            logger.warning("open-arb %s failed: %s", leg, e)
            return {"leg": leg, "ok": False, "error": str(e)}

    long_res, short_res = await asyncio.gather(
        _one("long",  body.long_wallet_id,  body.long_quantity,
             body.long_leverage,  body.long_margin_mode,  "buy"),
        _one("short", body.short_wallet_id, body.short_quantity,
             body.short_leverage, body.short_margin_mode, "sell"),
    )
    return {"long": long_res, "short": short_res,
            "fully_filled": long_res["ok"] and short_res["ok"]}


# ── Write ─────────────────────────────────────────────────────────────────────
class OpenOrderIn(BaseModel):
    wallet_id: int
    symbol: str
    side: str = Field(..., pattern="^(buy|sell)$")
    quantity: float = Field(..., gt=0, le=1_000_000)
    leverage: int = Field(3, ge=1, le=125)
    margin_mode: str = Field("isolated", pattern="^(isolated|cross)$")

    @field_validator("symbol", mode="before")
    @classmethod
    def _v(cls, v): return _vsym(v)


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
    side: str | None = Field(None, pattern="^(buy|sell)$")

    @field_validator("symbol", mode="before")
    @classmethod
    def _v(cls, v): return _vsym(v)


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


# ── Enable/disable trading on a wallet (switch purpose) ──────────────────────
class ToggleIn(BaseModel):
    can_trade: bool


@router.patch("/wallets/{wallet_id}")
def toggle_can_trade(
    wallet_id: int,
    body: ToggleIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from backend.db.models import Wallet as W
    w = db.query(Wallet).filter(Wallet.id == wallet_id, Wallet.user_id == user.id).first()
    if not w:
        raise HTTPException(404, "Wallet not found")
    if w.wallet_type != "exchange":
        raise HTTPException(400, "Trading can only be enabled on exchange wallets")
    if body.can_trade:
        from backend.services.trade_adapters import TRADE_SUPPORTED
        if w.type_value not in TRADE_SUPPORTED:
            raise HTTPException(400, f"Trading on {w.type_value} is not yet supported.")
        # Only one trading-eligible (screener|both) key per exchange per user
        dup = (
            db.query(W)
            .filter(
                W.user_id == user.id,
                W.wallet_type == "exchange",
                W.type_value == w.type_value,
                W.purpose.in_(("screener", "both")),
                W.id != w.id,
                W.is_archived == False,  # noqa: E712
            )
            .first()
        )
        if dup:
            raise HTTPException(409, f"A screener-eligible key for {w.type_value} already exists. Switch it off first.")
        # If it was 'portfolio' before, preserve the portfolio role too → 'both'.
        # If it was already 'screener' (or 'both'), nothing to do.
        w.purpose = "both" if w.purpose == "portfolio" else ("both" if w.purpose == "both" else "screener")
        w.can_trade = True
    else:
        # Flipping screener off: if key was 'both', keep portfolio; else plain portfolio
        w.purpose = "portfolio"
        w.can_trade = False
    db.commit()
    return {"id": w.id, "can_trade": w.can_trade, "purpose": w.purpose}
