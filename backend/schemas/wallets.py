import enum
from pydantic import BaseModel, Field, field_validator, model_validator

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

class ChainWalletSchema(WalletBasicSchema):
    address: str = Field(
        ...,
        examples=["0x1234567890abcdef1234567890abcdef12345678"],
        description="Wallet address"
    )
    chain: ChainType = Field(
        ...,
        examples=["ethereum"],
        description="Blockchain type (e.g., ethereum, solana)"
    )

    @model_validator(mode="after")
    def validate_chain_address(self):
        if self.address.startswith("0x") and self.chain in (ChainType.TRON, ChainType.SOLANA):
            raise ValueError("Invalid Tron/Solana address (could not start with 0x)")
        if not self.address.startswith("0x") and self.chain not in (ChainType.TRON, ChainType.SOLANA):
            raise ValueError("Invalid EVM address (must start with 0x)")

        return self


class ExchangeWalletSchema(WalletBasicSchema):
    exchange: ExchangeType = Field(
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