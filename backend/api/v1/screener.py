from fastapi import APIRouter, Depends

from backend.api.deps import get_current_user
from backend.services.arbitrage_service import get_funding_data

router = APIRouter(prefix="/screener", tags=["screener"])


@router.get("/funding")
async def funding_rates(_=Depends(get_current_user)):
    """Funding rates across perpetual futures exchanges. Cached 30s per exchange."""
    return await get_funding_data()
