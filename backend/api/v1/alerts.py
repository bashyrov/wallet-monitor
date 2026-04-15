"""Arbitrage spread alerts CRUD."""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.api.deps import get_db, get_current_user
from backend.db.models import ArbAlert, User

router = APIRouter(prefix="/alerts", tags=["alerts"])
logger = logging.getLogger("avalant.alerts")


class AlertCreate(BaseModel):
    symbol: str
    long_exchange: str
    short_exchange: str
    threshold: float        # spread % to trigger
    direction: str = "any"  # any | above | below


class AlertOut(BaseModel):
    id: int
    symbol: str
    long_exchange: str
    short_exchange: str
    threshold: float
    direction: str
    enabled: bool
    last_triggered_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("", response_model=list[AlertOut])
def list_alerts(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(ArbAlert).filter(ArbAlert.user_id == current_user.id).all()


@router.post("", response_model=AlertOut, status_code=201)
def create_alert(body: AlertCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if body.direction not in ("any", "above", "below"):
        raise HTTPException(400, "direction must be any|above|below")
    alert = ArbAlert(
        user_id=current_user.id,
        symbol=body.symbol.upper(),
        long_exchange=body.long_exchange.lower(),
        short_exchange=body.short_exchange.lower(),
        threshold=body.threshold,
        direction=body.direction,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    logger.info("Alert created id=%d by user %d", alert.id, current_user.id)
    return alert


@router.patch("/{alert_id}", response_model=AlertOut)
def update_alert(alert_id: int, body: AlertCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    alert = db.query(ArbAlert).filter(ArbAlert.id == alert_id, ArbAlert.user_id == current_user.id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")
    alert.symbol = body.symbol.upper()
    alert.long_exchange = body.long_exchange.lower()
    alert.short_exchange = body.short_exchange.lower()
    alert.threshold = body.threshold
    alert.direction = body.direction
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


@router.delete("/{alert_id}", status_code=204)
def delete_alert(alert_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    alert = db.query(ArbAlert).filter(ArbAlert.id == alert_id, ArbAlert.user_id == current_user.id).first()
    if not alert:
        raise HTTPException(404, "Alert not found")
    db.delete(alert)
    db.commit()
