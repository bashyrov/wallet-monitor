"""Arbitrage spread alerts CRUD."""
import logging
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from backend.api.deps import get_db, get_current_user
from backend.db.models import ArbAlert, User

router = APIRouter(prefix="/alerts", tags=["alerts"])
logger = logging.getLogger("avalant.alerts")

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,16}$")
_KNOWN_EX = {
    "binance", "bybit", "okx", "gate", "kucoin", "mexc", "bitget",
    "hyperliquid", "aster", "ethereal", "whitebit", "bingx", "lighter", "paradex",
}


class AlertCreate(BaseModel):
    symbol: str
    long_exchange: str
    short_exchange: str
    threshold: float = Field(..., gt=0, le=50)
    direction: str = Field("any", pattern="^(any|above|below)$")
    mode: str = Field("futures", pattern="^(futures|spot|dex)$")
    trigger_mode: str = Field("speed", pattern="^(speed|protected)$")

    @field_validator("symbol", mode="before")
    @classmethod
    def _sym(cls, v):
        s = str(v or "").strip().upper()
        if not _SYMBOL_RE.match(s):
            raise ValueError("symbol must be 1-16 alphanumeric uppercase")
        return s

    @field_validator("long_exchange", "short_exchange", mode="before")
    @classmethod
    def _ex(cls, v):
        s = str(v or "").strip().lower()
        if s in ("*", "any", ""):
            return "*"
        if s not in _KNOWN_EX:
            raise ValueError(f"unknown exchange: {v!r}")
        return s


class AlertOut(BaseModel):
    id: int
    symbol: str
    long_exchange: str
    short_exchange: str
    threshold: float
    direction: str
    mode: Optional[str] = "futures"
    trigger_mode: Optional[str] = "speed"
    enabled: bool
    last_triggered_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("", response_model=list[AlertOut])
def list_alerts(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(ArbAlert).filter(ArbAlert.user_id == current_user.id).all()


@router.post("", response_model=AlertOut, status_code=201)
def create_alert(body: AlertCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.long_exchange != "*" and body.short_exchange != "*" and body.long_exchange == body.short_exchange:
        raise HTTPException(400, "long_exchange and short_exchange must differ")
    alert = ArbAlert(
        user_id=current_user.id,
        symbol=body.symbol,
        long_exchange=body.long_exchange,
        short_exchange=body.short_exchange,
        threshold=body.threshold,
        direction=body.direction,
        mode=body.mode,
        trigger_mode=body.trigger_mode,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    logger.info("Alert created id=%d by user %d (trigger=%s)", alert.id, current_user.id, body.trigger_mode)
    return alert


@router.patch("/{alert_id}", response_model=AlertOut)
def update_alert(alert_id: int, body: AlertCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.long_exchange != "*" and body.short_exchange != "*" and body.long_exchange == body.short_exchange:
        raise HTTPException(400, "long_exchange and short_exchange must differ")
    alert = db.query(ArbAlert).filter(ArbAlert.id == alert_id, ArbAlert.user_id == current_user.id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")
    alert.symbol = body.symbol
    alert.long_exchange = body.long_exchange
    alert.short_exchange = body.short_exchange
    alert.threshold = body.threshold
    alert.direction = body.direction
    alert.mode = body.mode
    alert.trigger_mode = body.trigger_mode
    db.commit()
    db.refresh(alert)
    return alert


@router.patch("/{alert_id}/toggle", response_model=AlertOut)
def toggle_alert(alert_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    alert = db.query(ArbAlert).filter(ArbAlert.id == alert_id, ArbAlert.user_id == current_user.id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")
    alert.enabled = not alert.enabled
    db.commit()
    db.refresh(alert)
    return alert


class TokenAlertCreate(BaseModel):
    """Shortcut to create a 'fires on any exchange pair' alert for a symbol."""
    symbol: str
    threshold: float = Field(..., gt=0, le=50)
    direction: str = Field("any", pattern="^(any|above|below)$")
    mode: str = Field("futures", pattern="^(futures|spot|dex)$")
    trigger_mode: str = Field("speed", pattern="^(speed|protected)$")

    @field_validator("symbol", mode="before")
    @classmethod
    def _sym(cls, v):
        s = str(v or "").strip().upper()
        if not _SYMBOL_RE.match(s):
            raise ValueError("symbol must be 1-16 alphanumeric uppercase")
        return s


@router.post("/token", response_model=AlertOut, status_code=201)
def create_token_alert(
    body: TokenAlertCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.query(ArbAlert).filter(
        ArbAlert.user_id == current_user.id,
        ArbAlert.symbol == body.symbol,
        ArbAlert.long_exchange == "*",
        ArbAlert.short_exchange == "*",
        (ArbAlert.mode == body.mode) | (ArbAlert.mode == None),  # noqa: E711
    ).delete(synchronize_session=False)
    alert = ArbAlert(
        user_id=current_user.id,
        symbol=body.symbol,
        long_exchange="*",
        short_exchange="*",
        threshold=body.threshold,
        direction=body.direction,
        mode=body.mode,
        trigger_mode=body.trigger_mode,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    logger.info("Token alert created id=%d by user %d for %s (trigger=%s)",
                alert.id, current_user.id, body.symbol, body.trigger_mode)
    return alert


@router.delete("/{alert_id}", status_code=204)
def delete_alert(alert_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    alert = db.query(ArbAlert).filter(ArbAlert.id == alert_id, ArbAlert.user_id == current_user.id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")
    db.delete(alert)
    db.commit()
