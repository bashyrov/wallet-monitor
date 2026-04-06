from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/providers")
def provider_counts():
    """Public endpoint — returns how many providers of each type are supported."""
    from backend.api.v1.wallets import WALLET_OPTIONS
    opts = WALLET_OPTIONS
    return {
        "exchanges": len(opts["exchange_types"]),
        "chains": len(opts["chain_types"]),
        "perp_dexes": sum(1 for p in opts["perpdex_types"] if not p.get("soon")),
    }
