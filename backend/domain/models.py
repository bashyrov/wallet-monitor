from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from backend.domain.enums import ChainType, ExchangeType

@dataclass
class WalletBasic(ABC):
    name: str
    user: str
    provider: object = field(init=False)

    def __post_init__(self):
        self.provider = self._resolve_provider()

    @abstractmethod
    def _resolve_provider(self) -> str:
        pass


@dataclass
class ChainWallet(WalletBasic):
    address: str
    chain: ChainType

    def _resolve_provider(self, ) -> str:
        from backend.providers.chains import CHAIN_PROVIDERS
        provider = CHAIN_PROVIDERS.get(self.chain)
        if not provider:
            raise ValueError(f"Unsupported exchange: {self.chain}")
        return provider


@dataclass
class ExchangeWallet(WalletBasic):
    exchange: ExchangeType
    api_key: str
    api_secret: str
    api_passphrase: str | None = None

    def _resolve_provider(self, ) -> str:
        from backend.providers.exchanges import EXCHANGE_PROVIDERS
        provider = EXCHANGE_PROVIDERS.get(self.exchange)
        if not provider:
            raise ValueError(f"Unsupported exchange: {self.exchange}")
        return provider


@dataclass
class BalanceResult:
    wallet: ChainWallet|ExchangeWallet
    provider: str
    totals: dict | None = None
    details: dict | None = None
