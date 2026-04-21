import asyncio
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.api.deps import get_db, get_current_user
from backend.db.models import Wallet, User, ProviderErrorLog, BalanceHistory
from backend.schemas.portfolio import BalanceFetchRequest, BalanceResponse, TransactionFetchRequest, TransactionResponse
from backend.services.balance_service import fetch_balances, fetch_balances_stream
from backend.services.transaction_service import fetch_transactions

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.post("/balance", response_model=BalanceResponse)
async def check_balance(
    body: BalanceFetchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Wallet).filter(Wallet.user_id == current_user.id)
    if body.wallet_ids:
        query = query.filter(Wallet.id.in_(body.wallet_ids))
    wallets = query.all()

    if not wallets:
        raise HTTPException(status_code=404, detail="No wallets found")

    try:
        current_user.request_count = (current_user.request_count or 0) + len(wallets)
        current_user.last_active_at = datetime.utcnow()
        db.commit()
    except Exception:
        db.rollback()

    return await fetch_balances(wallets, db)


@router.post("/balance/stream")
async def check_balance_stream(
    body: BalanceFetchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Wallet).filter(Wallet.user_id == current_user.id)
    if body.wallet_ids:
        query = query.filter(Wallet.id.in_(body.wallet_ids))
    wallets = query.all()

    if not wallets:
        raise HTTPException(status_code=404, detail="No wallets found")

    try:
        current_user.request_count = (current_user.request_count or 0) + len(wallets)
        current_user.last_active_at = datetime.utcnow()
        db.commit()
    except Exception:
        db.rollback()

    async def generate():
        async for event in fetch_balances_stream(wallets, db):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/transactions", response_model=TransactionResponse)
async def get_transactions(
    body: TransactionFetchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    wallet = db.query(Wallet).filter(
        Wallet.id == body.wallet_id,
        Wallet.user_id == current_user.id,
    ).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    try:
        current_user.request_count = (current_user.request_count or 0) + 1
        current_user.last_active_at = datetime.utcnow()
        db.commit()
    except Exception:
        db.rollback()

    return await fetch_transactions(wallet)


_TX_ETYPE = {
    "Invalid API credentials":          "auth",
    "Provider unavailable":             "network",
    "Rate limit exceeded":              "rate_limit",
}

def _tx_etype(error_msg: str | None) -> str:
    if not error_msg:
        return "unknown"
    for prefix, etype in _TX_ETYPE.items():
        if error_msg.startswith(prefix):
            return etype
    return "unknown"


@router.post("/transactions/bulk", response_model=list[TransactionResponse])
async def check_transactions_bulk(
    body: BalanceFetchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Wallet).filter(Wallet.user_id == current_user.id, Wallet.is_archived == False)
    if body.wallet_ids:
        query = query.filter(Wallet.id.in_(body.wallet_ids))
    wallets = query.all()
    if not wallets:
        raise HTTPException(status_code=404, detail="No wallets found")

    try:
        current_user.request_count = (current_user.request_count or 0) + len(wallets)
        current_user.last_active_at = datetime.utcnow()
        db.commit()
    except Exception:
        db.rollback()

    raw = await asyncio.gather(*[fetch_transactions(w) for w in wallets], return_exceptions=True)

    now = datetime.utcnow()
    responses: list[TransactionResponse] = []
    for wallet, result in zip(wallets, raw):
        if isinstance(result, Exception):
            resp = TransactionResponse(
                wallet_id=wallet.id, wallet_name=wallet.name,
                wallet_type=wallet.wallet_type, type_value=wallet.type_value,
                transactions=[], error="Failed to fetch — try again later",
            )
        else:
            resp = result

        if resp.error:
            try:
                db.add(ProviderErrorLog(
                    wallet_type=wallet.wallet_type, type_value=wallet.type_value,
                    error_type=_tx_etype(resp.error), created_at=now,
                ))
                db.commit()
            except Exception:
                db.rollback()

        responses.append(resp)

    return responses


@router.get("/history")
def get_balance_history(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    since = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(BalanceHistory)
        .filter(
            BalanceHistory.user_id == current_user.id,
            BalanceHistory.snapshot_at >= since,
        )
        .order_by(BalanceHistory.snapshot_at.asc())
        .all()
    )
    return [
        {
            "usd_total": r.usd_total,
            "at": r.snapshot_at.strftime("%Y-%m-%d %H:%M"),
            "totals": r.totals or {},
        }
        for r in rows
    ]
