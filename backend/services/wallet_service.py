"""CRUD operations for wallet and tag records."""
from sqlalchemy.orm import Session

from backend.crypto import encrypt_credentials, decrypt_credentials
from backend.db.models import Wallet, Tag, WalletAddress, User
from backend.domain.errors import WalletNotFound, TagNotFound, TagAlreadyExists, TagLimitReached
from backend.services import plan_service
from backend.schemas.common import WalletCreate, WalletUpdate, WalletOut, TagCreate, TagUpdate, TagOut, WalletAddressCreate, WalletAddressOut


def _build_perpdex_creds(body: WalletCreate) -> dict:
    """Per-DEX credential mapping. Each adapter has its own naming conventions
    — we store under the names the adapters already consume (see
    backend/services/trade_adapters/<venue>.py for the truth of which
    creds dict key each adapter reads).

    Storage layout per venue:
      - hyperliquid / ethereal: address (read) + api_secret=private_key (trade)
      - lighter:                api_key=account_index + api_secret=hex_private_key
                                + api_passphrase=api_key_index (default "255")
      - paradex:                address (read) + api_token=JWT (auth)
                                + private_key=l2_private_key (trade signing)
      - extended:               address only (read-only venue)
    """
    tv = (body.type_value or "").lower()
    creds: dict = {}
    if body.address:
        creds["address"] = body.address.strip()

    if tv == "lighter":
        # Lighter uses api_key as numeric account_index and api_passphrase as
        # api_key_index (default "255"). hex private key in api_secret.
        if body.account_index:
            creds["api_key"] = body.account_index.strip()
        if body.private_key:
            creds["api_secret"] = body.private_key.strip()
        # api_key_index defaults to "255" if user leaves it blank but supplied
        # a private key — every Lighter SDK example uses 255.
        idx = (body.api_key_index or "").strip()
        if not idx and body.private_key:
            idx = "255"
        if idx:
            creds["api_passphrase"] = idx
    elif tv == "paradex":
        if body.api_token:
            creds["api_token"] = body.api_token.strip()
        if body.l2_private_key:
            # Paradex Stark L2 priv-key. Stored as private_key — adapter reads
            # it directly. (Aster also uses api_secret as PK; Paradex needs the
            # explicit name because api_token already occupies that mental slot.)
            creds["private_key"] = body.l2_private_key.strip()
        if body.api_passphrase:
            # Subkey path: api_passphrase carries the subkey public key
            # (the L2 stark pubkey of the trading-only key registered in
            # Paradex's UI). When set, Go routes auth through
            # /v1/auth/{pubkey} and signs with the subkey priv-key in
            # private_key. The main account address still goes in
            # PARADEX-STARKNET-ACCOUNT (i.e. our `address` field).
            creds["api_passphrase"] = body.api_passphrase.strip()
    elif tv in ("hyperliquid", "ethereal"):
        # Both adapters accept either `private_key` or `api_secret` as the
        # EVM signing key. Store as api_secret to keep storage uniform with
        # CEX-style schemas.
        if body.private_key:
            creds["api_secret"] = body.private_key.strip()
    elif tv == "extended":
        # Extended (StarkEx perpetuals) — 4 fields:
        #   address         = Stark L2 public key (stored above)
        #   api_key         = API key from Extended UI (HTTP auth on GETs)
        #   private_key     = Stark L2 private key (signs orders)
        #   api_passphrase  = vault / collateral_position_id (decimal int)
        if body.api_key:
            creds["api_key"] = body.api_key.strip()
        if body.l2_private_key:
            creds["private_key"] = body.l2_private_key.strip()
        elif body.private_key:
            creds["private_key"] = body.private_key.strip()
        if body.api_passphrase:
            creds["api_passphrase"] = body.api_passphrase.strip()
    return creds


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
        is_archived=bool(wallet.is_archived),
        can_trade=bool(wallet.can_trade),
        purpose=wallet.purpose or "portfolio",
        is_main=bool(getattr(wallet, "is_main", False)),
        created_at=wallet.created_at,
        tags=[TagOut(id=t.id, name=t.name, color=t.color) for t in wallet.tags],
        addresses=[WalletAddressOut(id=a.id, wallet_id=a.wallet_id, name=a.name, address=a.address)
                   for a in wallet.addresses],
    )


# ── Limit enforcement helpers ─────────────────────────────────────────────────
def _portfolio_count(db: Session, user_id: int) -> int:
    return (
        db.query(Wallet)
        .filter(
            Wallet.user_id == user_id,
            Wallet.is_archived == False,  # noqa: E712
            Wallet.purpose.in_(("portfolio", "both")),
        )
        .count()
    )


def _exchange_keys_for_venue(db: Session, user_id: int, type_value: str) -> int:
    return (
        db.query(Wallet)
        .filter(
            Wallet.user_id == user_id,
            Wallet.is_archived == False,  # noqa: E712
            Wallet.wallet_type == "exchange",
            Wallet.type_value == type_value,
        )
        .count()
    )


def _has_main_for_venue(db: Session, user_id: int, type_value: str) -> bool:
    return (
        db.query(Wallet)
        .filter(
            Wallet.user_id == user_id,
            Wallet.is_archived == False,  # noqa: E712
            Wallet.wallet_type == "exchange",
            Wallet.type_value == type_value,
            Wallet.is_main == True,  # noqa: E712
        )
        .count()
    ) > 0


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


def unarchive_wallet(db: Session, wallet_id: int, user_id: int, user: User | None = None) -> WalletOut:
    wallet = db.query(Wallet).filter(
        Wallet.id == wallet_id, Wallet.user_id == user_id
    ).first()
    if not wallet:
        raise WalletNotFound(wallet_id)
    if user is None:
        user = db.query(User).filter(User.id == user_id).first()
    limits = plan_service.effective_limits(db, user)
    if (wallet.purpose or "portfolio") in ("portfolio", "both"):
        if not limits.portfolio_unlimited and _portfolio_count(db, user_id) >= limits.portfolio_limit:
            from backend.domain.errors import WalletLimitReached
            raise WalletLimitReached(limits.portfolio_limit)
    wallet.is_archived = False
    db.commit()
    db.refresh(wallet)
    return wallet_to_out(wallet)


def create_wallet(db: Session, body: WalletCreate, user_id: int, user: User | None = None) -> WalletOut:
    # Lock the user row for the duration of this transaction. Postgres
    # serializes concurrent /api/wallets POSTs for the same user — without
    # this, two parallel requests both saw count<limit, both inserted, and
    # the user ended up over the portfolio_limit / exchange_keys_per_venue
    # cap. SQLite ignores `with_for_update` (no row-level locks) but local
    # dev runs single-process anyway.
    locked_user = (
        db.query(User)
        .filter(User.id == user_id)
        .with_for_update()
        .first()
    )
    if locked_user is None:
        from backend.domain.errors import WalletLimitReached
        raise WalletLimitReached(0)
    if user is None:
        user = locked_user
    limits = plan_service.effective_limits(db, user)

    # Perp-DEX wallets are a single private-key/credential identity that serves
    # BOTH viewing and trading by design — there's no separate "read-only key"
    # like on CEX. Default them to 'both' so they participate in trade lookups
    # immediately. User can downgrade via PATCH.
    if body.wallet_type == "exchange":
        purpose = body.purpose
    elif body.wallet_type == "perpdex":
        purpose = body.purpose if body.purpose != "portfolio" else "both"
    else:
        purpose = "portfolio"

    # Portfolio limit only applies when the new wallet would count as portfolio.
    if purpose in ("portfolio", "both"):
        if not limits.portfolio_unlimited and _portfolio_count(db, user_id) >= limits.portfolio_limit:
            from backend.domain.errors import WalletLimitReached
            raise WalletLimitReached(limits.portfolio_limit)

    # Per-venue exchange-key cap: 1 for free / unlimited for paid (configurable
    # per plan; -1 means unlimited). Existing wallets are always grandfathered —
    # the cap is checked before *adding* a new one, never retroactively.
    if body.wallet_type == "exchange" and not limits.keys_unlimited:
        existing_for_venue = _exchange_keys_for_venue(db, user_id, body.type_value)
        if existing_for_venue >= limits.exchange_keys_per_venue:
            from backend.domain.errors import DuplicateScreenerKey
            raise DuplicateScreenerKey(body.type_value, 0)

    if body.wallet_type == "exchange" or (body.wallet_type == "perpdex" and body.type_value == "aster"):
        raw_creds = {
            "api_key": body.api_key.strip(),
            "api_secret": body.api_secret.strip(),
        }
        if body.api_passphrase:
            raw_creds["api_passphrase"] = body.api_passphrase.strip()
    elif body.wallet_type == "perpdex":
        raw_creds = _build_perpdex_creds(body)
    else:
        raw_creds = {"address": body.address.strip()}

    # First exchange key for a venue is automatically the main one — second
    # and third additions stay non-main until the user explicitly switches.
    auto_main = (
        body.wallet_type == "exchange"
        and not _has_main_for_venue(db, user_id, body.type_value)
    )

    wallet = Wallet(
        name=body.name,
        wallet_type=body.wallet_type,
        type_value=body.type_value,
        credentials=encrypt_credentials(raw_creds),
        user_id=user_id,
        purpose=purpose,
        can_trade=(purpose in ("screener", "both")),
        is_main=auto_main,
    )
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    return wallet_to_out(wallet)


def set_main_wallet(db: Session, wallet_id: int, user_id: int) -> WalletOut:
    """Designate this exchange wallet as the main trading key for its venue.

    Clears `is_main` on every other wallet (same user, same venue) and sets
    it on the target. No-ops cleanly if the wallet is already main.
    """
    wallet = _get_wallet(db, wallet_id, user_id)
    if wallet.wallet_type != "exchange":
        raise ValueError("only exchange wallets can be marked as main")
    db.query(Wallet).filter(
        Wallet.user_id == user_id,
        Wallet.wallet_type == "exchange",
        Wallet.type_value == wallet.type_value,
        Wallet.id != wallet.id,
    ).update({Wallet.is_main: False}, synchronize_session=False)
    wallet.is_main = True
    db.commit()
    db.refresh(wallet)
    return wallet_to_out(wallet)


def update_wallet(db: Session, wallet_id: int, body: WalletUpdate, user_id: int) -> WalletOut:
    wallet = _get_wallet(db, wallet_id, user_id)
    if body.name is not None:
        wallet.name = body.name

    creds = decrypt_credentials(wallet.credentials or {})
    if wallet.wallet_type == "exchange" or (wallet.wallet_type == "perpdex" and wallet.type_value == "aster"):
        if body.api_key:
            creds["api_key"] = body.api_key.strip()
        if body.api_secret:
            creds["api_secret"] = body.api_secret.strip()
        if body.api_passphrase is not None:
            if body.api_passphrase:
                creds["api_passphrase"] = body.api_passphrase.strip()
            else:
                creds.pop("api_passphrase", None)
    elif wallet.wallet_type == "perpdex":
        # Trade-credential updates per perpdex venue. Keys are stored under
        # the names the adapter consumes (see _build_perpdex_creds).
        tv = (wallet.type_value or "").lower()
        if body.address:
            creds["address"] = body.address.strip()
        if tv == "lighter":
            if body.account_index:
                creds["api_key"] = body.account_index.strip()
            if body.private_key:
                creds["api_secret"] = body.private_key.strip()
            if body.api_key_index:
                creds["api_passphrase"] = body.api_key_index.strip()
        elif tv == "paradex":
            if body.api_token:
                creds["api_token"] = body.api_token.strip()
            if body.l2_private_key:
                creds["private_key"] = body.l2_private_key.strip()
            if body.api_passphrase:
                creds["api_passphrase"] = body.api_passphrase.strip()
        elif tv in ("hyperliquid", "ethereal"):
            if body.private_key:
                creds["api_secret"] = body.private_key.strip()
    else:
        if body.address:
            creds["address"] = body.address.strip()

    # Purpose switch — for credentialed wallet types (exchange + perpdex).
    # Screener-eligibility is a per-exchange unique constraint so flipping to
    # screener/both here enforces the same rule as toggle_can_trade.
    if body.purpose is not None and wallet.wallet_type in ("exchange", "perpdex"):
        from backend.services.trade_adapters import TRADE_SUPPORTED
        from backend.services import trade_proxy
        needs_trade = body.purpose in ("screener", "both")
        # A venue counts as "trade-supported" if EITHER:
        #   - the local Python adapter trades it (TRADE_SUPPORTED), OR
        #   - it's on the Go-engine cutover list (trade_proxy.is_enabled)
        # Paradex is a Python-readonly proxy but trades fine via Go when
        # GO_TRADE_VENUES includes it; without this branch the upgrade
        # path artificially refuses.
        if needs_trade and wallet.type_value not in TRADE_SUPPORTED \
                and not trade_proxy.is_enabled(wallet.type_value):
            raise ValueError(f"Trading on {wallet.type_value} is not supported yet.")
        if needs_trade:
            dup = (
                db.query(Wallet)
                .filter(
                    Wallet.user_id == user_id,
                    Wallet.wallet_type == wallet.wallet_type,
                    Wallet.type_value == wallet.type_value,
                    Wallet.purpose.in_(("screener", "both")),
                    Wallet.id != wallet.id,
                    Wallet.is_archived == False,  # noqa: E712
                )
                .first()
            )
            if dup:
                raise ValueError(f"A screener-eligible key for {wallet.type_value} already exists. Switch it off first.")
        wallet.purpose = body.purpose
        wallet.can_trade = needs_trade

    wallet.credentials = encrypt_credentials(creds)
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


FREE_TAG_LIMIT = 5

# ── Tags ──────────────────────────────────────────────────────────────────────

def list_tags(db: Session, user_id: int) -> list[Tag]:
    """Return system tags (user_id IS NULL) + current user's tags."""
    return (
        db.query(Tag)
        .filter((Tag.user_id == None) | (Tag.user_id == user_id))
        .order_by(Tag.name)
        .all()
    )


def create_tag(db: Session, body: TagCreate, user_id: int) -> Tag:
    # Enforce limit (system tags don't count)
    user_tag_count = db.query(Tag).filter(Tag.user_id == user_id).count()
    if user_tag_count >= FREE_TAG_LIMIT:
        raise TagLimitReached(FREE_TAG_LIMIT)
    # Check name uniqueness within user's scope
    existing = db.query(Tag).filter(Tag.name == body.name, Tag.user_id == user_id).first()
    if existing:
        raise TagAlreadyExists(body.name)
    tag = Tag(name=body.name, color=body.color, user_id=user_id)
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


def update_tag(db: Session, tag_id: int, body: TagUpdate, user_id: int) -> Tag:
    tag = db.query(Tag).filter(Tag.id == tag_id, Tag.user_id == user_id).first()
    if not tag:
        raise TagNotFound(tag_id)
    if body.name is not None:
        conflict = db.query(Tag).filter(
            Tag.name == body.name, Tag.user_id == user_id, Tag.id != tag_id
        ).first()
        if conflict:
            raise TagAlreadyExists(body.name)
        tag.name = body.name
    if body.color is not None:
        tag.color = body.color
    db.commit()
    db.refresh(tag)
    return tag


def delete_tag(db: Session, tag_id: int, user_id: int) -> None:
    tag = db.query(Tag).filter(Tag.id == tag_id, Tag.user_id == user_id).first()
    if not tag:
        raise TagNotFound(tag_id)
    db.delete(tag)
    db.commit()
