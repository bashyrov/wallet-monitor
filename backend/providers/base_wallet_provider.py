from abc import abstractmethod, ABC

from backend.domain import ChainWallet
from backend.domain.models import BalanceResult, ExchangeWallet


class BaseWalletProvider(ABC):

    name: str
    label: str = ""
    enabled: bool = True

    @abstractmethod
    async def fetch_balance(self, wallet: ExchangeWallet | ChainWallet) -> BalanceResult:
        raise NotImplementedError

    @staticmethod
    def _empty_details():
        return {
            "spot": {},
            "futures": {},
            "earn": {},
        }

    @staticmethod
    def _build_result(wallet, provider, spot, futures, earn):
        from collections import defaultdict
        from decimal import Decimal

        totals = defaultdict(Decimal)

        for bucket in (spot, futures, earn):
            for asset, amt in bucket.items():
                totals[asset] += amt

        return BalanceResult(
            wallet=wallet,
            provider=provider,
            totals={k: str(v) for k, v in totals.items() if v != 0},
            details={
                "spot": {k: str(v) for k, v in spot.items() if v != 0},
                "futures": {k: str(v) for k, v in futures.items() if v != 0},
                "earn": {k: str(v) for k, v in earn.items() if v != 0},
            },
        )
