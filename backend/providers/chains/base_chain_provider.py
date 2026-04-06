import asyncio
from collections import defaultdict
from decimal import Decimal
import httpx
from backend.providers.http import RetryClient

from backend.domain.models import BalanceResult


class BaseChainProvider:
    name = "BaseChainProvider"
    label: str = ""
    enabled: bool = True
    base_url: str = ""

    def __init__(self):
        self._client = RetryClient(timeout=20.0)

    async def aclose(self):
        await self._client.aclose()

    @staticmethod
    def _d(value) -> Decimal:
        if value in (None, "", False):
            return Decimal("0")
        return Decimal(str(value))

    async def fetch_balance(self, wallet) -> BalanceResult:
        """
        Этот метод должны реализовать наследники
        """
        raise NotImplementedError("fetch_balance must be implemented in subclass")