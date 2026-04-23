import asyncio
from collections import defaultdict
from decimal import Decimal

import httpx
from backend.providers.http import RetryClient

from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider


class ParadexProvider(BaseWalletProvider):
    name = "ParadexProvider"
    label = "Paradex"
    enabled = True
    # Paradex uses Starknet signature auth → JWT. We declare it as an
    # `api_token` credential (not a traditional api_key/secret pair).
    needs_api_key = False
    needs_api_token = True
    base_url = "https://api.prod.paradex.trade"

    def __init__(self):
        self._client = RetryClient(timeout=20.0)

    async def aclose(self):
        await self._client.aclose()

    @staticmethod
    def _d(value):
        if value in (None, "", False):
            return Decimal("0")
        return Decimal(str(value))

    async def fetch_balance(self, wallet) -> BalanceResult:
        jwt_token = getattr(wallet, "api_token", None) or getattr(wallet, "jwt_token", None)
        if not jwt_token:
            raise ValueError("Paradex wallet requires api_token or jwt_token")

        try:
            url = f"{self.base_url}/v1/balance"
            headers = {"accept": "application/json", "authorization": f"Bearer {jwt_token}"}

            resp = await self._client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") or []

            totals = defaultdict(Decimal)
            for item in results:
                token = (item.get("token") or "").strip().upper()
                size = self._d(item.get("size"))
                if token and size > 0:
                    totals[token] += size

            filtered_assets = {k: str(v) for k, v in totals.items() if v > 0}

            return BalanceResult(wallet=wallet, provider=self.name, totals=filtered_assets, details={"assets": filtered_assets})

        except Exception as e:
            print(f"Error fetching Paradex balance: {e}")
            return BalanceResult(wallet=wallet, provider=self.name, totals={}, details={"assets": {}})

        finally:
            await self.aclose()