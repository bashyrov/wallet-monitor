from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ── Tags ──────────────────────────────────────────────────────────────────────

class TagCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    color: str = Field(default="#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")


class TagUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=50)
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")


class TagOut(BaseModel):
    id: int
    name: str
    color: str

    model_config = {"from_attributes": True}


# ── Wallets ───────────────────────────────────────────────────────────────────

class WalletCreate(BaseModel):
    name: str = Field(..., min_length=6)
    wallet_type: Literal["exchange", "chain", "perpdex"]
    type_value: str
    # 'portfolio' = read-only balance/positions, 'screener' = trading,
    # 'both' = the same key serves both purposes. (exchange wallets only)
    purpose: Literal["portfolio", "screener", "both"] = "portfolio"
    # exchange fields
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None
    # chain / perpdex fields
    address: str | None = None
    # perpdex-only (currently Paradex): signed-in JWT from paradex.trade.
    api_token: str | None = None


class WalletUpdate(BaseModel):
    name: str | None = Field(None, min_length=6)
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None
    address: str | None = None
    api_token: str | None = None
    purpose: Literal["portfolio", "screener", "both"] | None = None


class WalletAddressCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    address: str = Field(..., min_length=5, max_length=200)


class WalletAddressOut(BaseModel):
    id: int
    wallet_id: int
    name: str
    address: str

    model_config = {"from_attributes": True}


class WalletOut(BaseModel):
    id: int
    name: str
    wallet_type: str
    type_value: str
    display_info: str  # masked api_key or address
    is_archived: bool = False
    can_trade: bool = False
    purpose: str = "portfolio"
    created_at: datetime
    tags: list[TagOut] = []
    addresses: list[WalletAddressOut] = []

    model_config = {"from_attributes": True}
