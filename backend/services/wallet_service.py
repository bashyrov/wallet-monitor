"""CRUD operations for wallet and tag records."""
from sqlalchemy.orm import Session

from backend.crypto import encrypt_credentials, decrypt_credentials
from backend.db.models import Wallet, Tag, WalletAddress
from backend.domain.errors import WalletNotFound, TagNotFound, TagAlreadyExists
from backend.schemas.common import WalletCreate, WalletOut, TagCreate, TagUpdate, TagOut, WalletAddressCreate, WalletAddressOut


def _display_info(wallet: Wallet) -> str:
    creds = decrypt_credentials(wallet.credentials or {})
    if wallet.wallet_type == "exchange" or (wallet.wallet_type == "perpdex" and wallet.type_value == "aster"):
        key = creds.get("api_key", "")
        if len(key) > 8:
            return key[:4] + "****" + key[-4:]
        return "****"
    return creds.get("address", "")


def wallet_to_out(wallet: Wallet) -> WalletOut:
    return WalletOut(
        id=wallet.id,
        name=wallet.name,
        wallet_type=wallet.wallet_type,
        type_value=wallet.type_value,
        display_info=_display_info(wallet),
        created_at=wallet.created_at,
        tags=[TagOut(id=t.id, name=t.name, color=t.color) for t in wallet.tags],
        addresses=[WalletAddressOut(id=a.id, wallet_id=a.wallet_id, name=a.name, address=a.address)
                   for a in wallet.addresses],
    )


def _get_wallet(db: Session, wallet_id: int, user_id: int) -> Wallet:
    """Get wallet by id, scoped to user."""
    wallet = db.query(Wallet).filter(
        Wallet.id == wallet_id, Wallet.user_id == user_id
    ).first()
    if not wallet:
        raise WalletNotFound(wallet_id)
    return wallet


# ── Wallets ───────────────────────────────────────────────────────────────────

def list_wallets(db: Session, user_id: int) -> list[WalletOut]:
    wallets = db.query(Wallet).filter(
        Wallet.user_id == user_id,
        Wallet.is_archived == False,
    ).order_by(Wallet.created_at.desc()).all()
    return [wallet_to_out(w) for w in wallets]


def list_archived_wallets(db: Session, user_id: int) -> list[WalletOut]:
    wallets = db.query(Wallet).filter(
        Wallet.user_id == user_id,
        Wallet.is_archived == True,
    ).order_by(Wallet.created_at.desc()).all()
    return [wallet_to_out(w) for w in wallets]


def archive_wallet(db: Session, wallet_id: int, user_id: int) -> WalletOut:
    wallet = _get_wallet(db, wallet_id, user_id)
    wallet.is_archived = True
    db.commit()
    db.refresh(wallet)
    return wallet_to_out(wallet)


def unarchive_wallet(db: Session, wallet_id: int, user_id: int, is_admin: bool = False) -> WalletOut:
    wallet = db.query(Wallet).filter(
        Wallet.id == wallet_id, Wallet.user_id == user_id
    ).first()
    if not wallet:
        raise WalletNotFound(wallet_id)
    if not is_admin:
        active_count = db.query(Wallet).filter(
            Wallet.user_id == user_id,
            Wallet.is_archived == False,
        ).count()
        if active_count >= FREE_WALLET_LIMIT:
            from backend.domain.errors import WalletLimitReached
            raise WalletLimitReached(FREE_WALLET_LIMIT)
    wallet.is_archived = False
    db.commit()
    db.refresh(wallet)
    return wallet_to_out(wallet)


FREE_WALLET_LIMIT = 4


def create_wallet(db: Session, body: WalletCreate, user_id: int, is_admin: bool = False) -> WalletOut:
    if not is_admin:
        count = db.query(Wallet).filter(
            Wallet.user_id == user_id, Wallet.is_archived == False
        ).count()
        if count >= FREE_WALLET_LIMIT:
            from backend.domain.errors import WalletLimitReached
            raise WalletLimitReached(FREE_WALLET_LIMIT)

    if body.wallet_type == "exchange" or (body.wallet_type == "perpdex" and body.type_value == "aster"):
        raw_creds = {
            "api_key": body.api_key.strip(),
            "api_secret": body.api_secret.strip(),
        }
        if body.api_passphrase:
            raw_creds["api_passphrase"] = body.api_passphrase.strip()
    else:
        raw_creds = {"address": body.address.strip()}

    wallet = Wallet(
        name=body.name,
        wallet_type=body.wallet_type,
        type_value=body.type_value,
        credentials=encrypt_credentials(raw_creds),
        user_id=user_id,
    )
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    return wallet_to_out(wallet)


def delete_wallet(db: Session, wallet_id: int, user_id: int) -> None:
    wallet = _get_wallet(db, wallet_id, user_id)
    db.delete(wallet)
    db.commit()


def add_tag(db: Session, wallet_id: int, tag_id: int, user_id: int) -> WalletOut:
    wallet = _get_wallet(db, wallet_id, user_id)
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise TagNotFound(tag_id)
    if tag not in wallet.tags:
        wallet.tags.append(tag)
        db.commit()
        db.refresh(wallet)
    return wallet_to_out(wallet)


def remove_tag(db: Session, wallet_id: int, tag_id: int, user_id: int) -> WalletOut:
    wallet = _get_wallet(db, wallet_id, user_id)
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise TagNotFound(tag_id)
    if tag in wallet.tags:
        wallet.tags.remove(tag)
        db.commit()
        db.refresh(wallet)
    return wallet_to_out(wallet)


# ── Wallet Addresses ──────────────────────────────────────────────────────────

def list_wallet_addresses(db: Session, wallet_id: int, user_id: int) -> list[WalletAddressOut]:
    _get_wallet(db, wallet_id, user_id)  # ownership check
    wallet = db.query(Wallet).filter(Wallet.id == wallet_id).first()
    return [WalletAddressOut(id=a.id, wallet_id=a.wallet_id, name=a.name, address=a.address)
            for a in wallet.addresses]


def create_wallet_address(db: Session, wallet_id: int, body: WalletAddressCreate, user_id: int) -> WalletAddressOut:
    wallet = _get_wallet(db, wallet_id, user_id)
    if wallet.wallet_type != "exchange":
        from backend.domain.errors import InvalidProviderType
        raise InvalidProviderType("Named addresses can only be added to exchange wallets")
    wa = WalletAddress(wallet_id=wallet_id, name=body.name.strip(), address=body.address.strip())
    db.add(wa)
    db.commit()
    db.refresh(wa)
    return WalletAddressOut(id=wa.id, wallet_id=wa.wallet_id, name=wa.name, address=wa.address)


def delete_wallet_address(db: Session, wallet_id: int, address_id: int, user_id: int) -> None:
    _get_wallet(db, wallet_id, user_id)  # ownership check
    wa = db.query(WalletAddress).filter(
        WalletAddress.id == address_id, WalletAddress.wallet_id == wallet_id
    ).first()
    if not wa:
        raise WalletNotFound(address_id)
    db.delete(wa)
    db.commit()


def all_addresses(db: Session, user_id: int) -> list[dict]:
    """Return all named addresses + chain/perpdex wallet addresses for the current user."""
    result = []
    wallets = db.query(Wallet).filter(Wallet.user_id == user_id).all()
    for w in wallets:
        creds = decrypt_credentials(w.credentials or {})
        for a in w.addresses:
            result.append({
                "address": a.address.lower(),
                "label": a.name,
                "wallet_name": w.name,
                "wallet_type": w.wallet_type,
                "type_value": w.type_value,
            })
        if w.wallet_type in ("chain", "perpdex"):
            addr = creds.get("address", "")
            if addr:
                result.append({
                    "address": addr.lower(),
                    "label": w.name,
                    "wallet_name": w.name,
                    "wallet_type": w.wallet_type,
                    "type_value": w.type_value,
                })
    return result


# ── Tags ──────────────────────────────────────────────────────────────────────

def list_tags(db: Session) -> list[Tag]:
    return db.query(Tag).order_by(Tag.name).all()


def create_tag(db: Session, body: TagCreate) -> Tag:
    existing = db.query(Tag).filter(Tag.name == body.name).first()
    if existing:
        raise TagAlreadyExists(body.name)
    tag = Tag(name=body.name, color=body.color)
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


def update_tag(db: Session, tag_id: int, body: TagUpdate) -> Tag:
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise TagNotFound(tag_id)
    if body.name is not None:
        conflict = db.query(Tag).filter(Tag.name == body.name, Tag.id != tag_id).first()
        if conflict:
            raise TagAlreadyExists(body.name)
        tag.name = body.name
    if body.color is not None:
        tag.color = body.color
    db.commit()
    db.refresh(tag)
    return tag


def delete_tag(db: Session, tag_id: int) -> None:
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise TagNotFound(tag_id)
    db.delete(tag)
    db.commit()
