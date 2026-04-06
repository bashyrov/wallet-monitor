from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.api.deps import get_db, get_current_user
from backend.db.models import Wallet, User
from backend.schemas.portfolio import BalanceFetchRequest, BalanceResponse, TransactionFetchRequest, TransactionResponse
from backend.services.balance_service import fetch_balances
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
        current_user.request_count = (current_user.request_count or 0) + 1
        db.commit()
    except Exception:
        db.rollback()

    return await fetch_balances(wallets)


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
        db.commit()
    except Exception:
        db.rollback()

    return await fetch_transactions(wallet)
