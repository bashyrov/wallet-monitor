from dataclasses import dataclass

from backend.domain.enums import ChainType, ExchangeType


@dataclass
class WalletBasic:
    name: str
    user: str

@dataclass
class ChainWallet(WalletBasic):
    address: str
    chain: ChainType

@dataclass
class ExchangeWallet(WalletBasic):
    exchange: ExchangeType
    api_key: str
    api_secret: str
    api_passphrase: str | None = None
