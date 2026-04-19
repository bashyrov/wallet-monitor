from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.api.deps import get_db, get_current_user
from backend.db.models import User
from backend.domain.errors import WalletNotFound, TagNotFound, InvalidProviderType, WalletLimitReached
from backend.schemas.common import WalletCreate, WalletUpdate, WalletOut, WalletAddressCreate, WalletAddressOut
import backend.services.wallet_service as svc

from backend.providers.exchanges import EXCHANGE_PROVIDERS
from backend.providers.perp_dexes import PERPDEX_PROVIDERS
from backend.providers.chains import CHAIN_META
from backend.services import admin_settings

router = APIRouter(prefix="/wallets", tags=["wallets"])


def _build_wallet_options() -> dict:
    disabled_ex = admin_settings.get_disabled_wallet_exchanges()
    disabled_ch = admin_settings.get_disabled_chains()
    disabled_pd = admin_settings.get_disabled_perpdexes()
    exchange_types = [
        {
            "value": value,
            "label": p.label,
            "needs_passphrase": getattr(p, "needs_passphrase", False),
        }
        for value, p in EXCHANGE_PROVIDERS.items()
        if isinstance(p, type) and getattr(p, "enabled", True) and value.lower() not in disabled_ex
    ]
    perpdex_types = [
        {
            "value": value,
            "label": p.label,
            "needs_api_key": getattr(p, "needs_api_key", False),
            **( {"soon": True} if getattr(p, "soon", False) else {} ),
        }
        for value, p in PERPDEX_PROVIDERS.items()
        if isinstance(p, type) and getattr(p, "enabled", True) and value.lower() not in disabled_pd
    ]
    chain_types = [
        {"value": value, "label": meta["label"]}
        for value, meta in CHAIN_META.items()
        if meta.get("enabled", True) and value.lower() not in disabled_ch
    ]
    return {
        "exchange_types": exchange_types,
        "chain_types": chain_types,
        "perpdex_types": perpdex_types,
    }


WALLET_OPTIONS = _build_wallet_options()  # snapshot for /api/health; live reads go through _build_wallet_options()


@router.get("", response_model=list[WalletOut])
def list_wallets(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.list_wallets(db, current_user.id)


@router.get("/options")
def get_options(current_user: User = Depends(get_current_user)):
    return _build_wallet_options()


@router.get("/all-addresses", response_model=list[dict])
def get_all_addresses(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.all_addresses(db, current_user.id)


@router.post("", response_model=WalletOut, status_code=201)
async def create_wallet(
    body: WalletCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Honour admin-disabled providers without leaking stale UI into the DB.
    tv = (body.type_value or "").lower()
    if body.wallet_type == "exchange" and tv in admin_settings.get_disabled_wallet_exchanges():
        raise HTTPException(status_code=403, detail=f"{body.type_value} is currently disabled by admin")
    if body.wallet_type == "chain" and tv in admin_settings.get_disabled_chains():
        raise HTTPException(status_code=403, detail=f"{body.type_value} chain is currently disabled by admin")
    if body.wallet_type == "perpdex" and tv in admin_settings.get_disabled_perpdexes():
        raise HTTPException(status_code=403, detail=f"{body.type_value} is currently disabled by admin")

    if body.wallet_type == "exchange" or (body.wallet_type == "perpdex" and body.type_value == "aster"):
        if not body.api_key or not body.api_secret:
            raise HTTPException(status_code=422, detail="api_key and api_secret are required")
        if body.wallet_type == "exchange":
            from backend.providers.exchanges import EXCHANGE_PROVIDERS
            prov = EXCHANGE_PROVIDERS.get(body.type_value)
            if prov is not None and getattr(prov, "needs_passphrase", False) and not body.api_passphrase:
                raise HTTPException(status_code=422, detail=f"{body.type_value} requires api_passphrase")

        # ── Live-validate the key against the exchange before saving ──
        if body.wallet_type == "exchange":
            from backend.services.trade_adapters import ADAPTERS, TRADE_SUPPORTED
            adapter = ADAPTERS.get(body.type_value)
            need_trade = body.purpose in ("screener", "both")
            if need_trade and body.type_value not in TRADE_SUPPORTED:
                raise HTTPException(
                    status_code=400,
                    detail=f"Screener trading on {body.type_value} is not yet supported. Add this key for Portfolio only."
                )
            if adapter is not None and hasattr(adapter, "validate_key"):
                creds = {"api_key": body.api_key.strip(), "api_secret": body.api_secret.strip()}
                if body.api_passphrase:
                    creds["api_passphrase"] = body.api_passphrase.strip()
                result = await adapter.validate_key(creds, need_trade=need_trade)
                if not result.get("can_read"):
                    raise HTTPException(status_code=400, detail=result.get("error") or "Key validation failed")
                if need_trade and not result.get("can_trade"):
                    raise HTTPException(status_code=400, detail=result.get("error") or "Key has no trading permission")
    elif body.wallet_type in ("chain", "perpdex"):
        if not body.address:
            raise HTTPException(status_code=422, detail="address is required for chain/perpdex wallets")
    else:
        raise HTTPException(status_code=422, detail="Invalid wallet_type")

    try:
        return svc.create_wallet(db, body, current_user.id, plan=getattr(current_user, 'plan', 'basic'))
    except WalletLimitReached as e:
        raise HTTPException(status_code=402, detail=str(e))
    except Exception as e:
        from backend.domain.errors import DuplicateScreenerKey
        if isinstance(e, DuplicateScreenerKey):
            raise HTTPException(status_code=409, detail=str(e))
        raise


@router.get("/archived", response_model=list[WalletOut])
def list_archived(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return svc.list_archived_wallets(db, current_user.id)


@router.post("/{wallet_id}/archive", response_model=WalletOut)
def archive_wallet(
    wallet_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return svc.archive_wallet(db, wallet_id, current_user.id)
    except WalletNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{wallet_id}/unarchive", response_model=WalletOut)
def unarchive_wallet(
    wallet_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return svc.unarchive_wallet(db, wallet_id, current_user.id, plan=getattr(current_user, 'plan', 'basic'))
    except WalletNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except WalletLimitReached as e:
        raise HTTPException(status_code=402, detail=str(e))


@router.patch("/{wallet_id}", response_model=WalletOut)
def update_wallet(
    wallet_id: int,
    body: WalletUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return svc.update_wallet(db, wallet_id, body, current_user.id)
    except WalletNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{wallet_id}", status_code=204)
def delete_wallet(
    wallet_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        svc.delete_wallet(db, wallet_id, current_user.id)
    except WalletNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{wallet_id}/tags/{tag_id}", response_model=WalletOut)
def add_tag(
    wallet_id: int, tag_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return svc.add_tag(db, wallet_id, tag_id, current_user.id)
    except (WalletNotFound, TagNotFound) as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{wallet_id}/tags/{tag_id}", response_model=WalletOut)
def remove_tag(
    wallet_id: int, tag_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return svc.remove_tag(db, wallet_id, tag_id, current_user.id)
    except (WalletNotFound, TagNotFound) as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Wallet Addresses ──────────────────────────────────────────────────────────

@router.get("/{wallet_id}/addresses", response_model=list[WalletAddressOut])
def list_addresses(
    wallet_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return svc.list_wallet_addresses(db, wallet_id, current_user.id)
    except WalletNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{wallet_id}/addresses", response_model=WalletAddressOut, status_code=201)
def add_address(
    wallet_id: int, body: WalletAddressCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return svc.create_wallet_address(db, wallet_id, body, current_user.id)
    except WalletNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidProviderType as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.delete("/{wallet_id}/addresses/{address_id}", status_code=204)
def delete_address(
    wallet_id: int, address_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        svc.delete_wallet_address(db, wallet_id, address_id, current_user.id)
    except WalletNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
