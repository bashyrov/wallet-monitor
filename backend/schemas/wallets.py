import enum
from pydantic import BaseModel, Field, field_validator

from backend.domain import ChainType, ExchangeType


class WalletBasicSchema(BaseModel):
    name: str = Field(
        ...,
        examples=["wallet1"],
        description="Name of the wallet"
    )
    user: str = Field(
        ...,
        examples=["user1"]
    ) #TODO: remove user from schema, get it from auth

    @field_validator("name")
    def validate_name(cls, value):
        if not value or len(value) < 6:
            raise ValueError("Name field must have minimum length of 6")
        return value

class ChainWalletSchema(BaseModel, WalletBasicSchema):
    address: str = Field(
        ...,
        examples=["0x1234567890abcdef1234567890abcdef12345678"],
        description="Wallet address"
    )
    chain: enum.Enum[ChainType] = Field(
        ...,
        examples=["ethereum"],
        description="Blockchain type (e.g., ethereum, solana)"
    )


class ExchangeWalletSchema(BaseModel, WalletBasicSchema):
    exchange: enum.Enum[ExchangeType] = Field(
        ...,
        examples=["binance"],
        description="Exchange type (e.g., binance, okx)"
    )
    api_key: str = Field(
        ...,
        examples=["your_api_key"],
        description="API key for the exchange"
    )
    api_secret: str = Field(
        ...,
        examples=["your_api_secret"],
        description="API secret for the exchange"
    )
    api_passphrase: str | None = Field(
        None,
        examples=["your_api_passphrase"],
        description="API passphrase for the exchange (if required)"
    )