from abc import abstractmethod, ABC

from backend.domain.models import BalanceResult


class BaseProvider(ABC):

    name: str

    @abstractmethod
    async def fetch_balance(self, wallet) -> BalanceResult:
        pass