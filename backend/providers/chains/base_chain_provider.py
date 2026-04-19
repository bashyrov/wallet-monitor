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

    # Shared across every chain-provider instance in this process. Without
    # this, each balance check was creating a fresh httpx client (TCP +
    # TLS handshake) for every single RPC/Ankr call, which drove up
    # latency and flaked out under parallel requests.
    _shared_client: RetryClient | None = None

    def __init__(self):
        if BaseChainProvider._shared_client is None:
            BaseChainProvider._shared_client = RetryClient(
                timeout=20.0,
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=40,
                    keepalive_expiry=30,
                ),
            )
        self._client = BaseChainProvider._shared_client

    async def aclose(self):
        # No-op: the shared client lives for the lifetime of the process.
        # (Closing here would break sibling instances mid-flight.)
        pass

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