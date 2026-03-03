from dataclasses import dataclass
from enum import Enum

from backend.domain.enums import ChainType, ExchangeType


@dataclass
class WalletBasic:
    name: str
    user: str

@dataclass
class ChainWallet(WalletBasic):
    address: str
    chain: Enum[ChainType]

@dataclass
class ExchangeWallet(WalletBasic):
    exchange: Enum[ExchangeType]
    api_key: str
    api_secret: str
    api_passphrase: str | None = None
