from collections import defaultdict
from decimal import Decimal
import asyncio

import requests

from backend.domain.models import BalanceResult
from backend.providers.base import BaseProvider


class ParadexProvider(BaseProvider):
    name = "ParadexProvider"
    base_url = "https://api.prod.paradex.trade"

    def __init__(self) -> None:
        self._session = requests.Session()

    async def aclose(self) -> None:
        self._session.close()

    @staticmethod
    def _d(value) -> Decimal:
        if value in (None, "", False):
            return Decimal("0")
        return Decimal(str(value))

    def _get_balances_sync(self, jwt_token: str) -> dict:
        url = f"{self.base_url}/v1/balance"
        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {jwt_token}",
        }

        response = self._session.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json()

    async def _get_balances(self, jwt_token: str) -> dict:
        return await asyncio.to_thread(self._get_balances_sync, jwt_token)

    async def fetch_balance(self, wallet) -> BalanceResult:
        jwt_token = getattr(wallet, "api_token", None) or getattr(wallet, "jwt_token", None)

        if not jwt_token:
            raise ValueError("Paradex wallet requires api_token or jwt_token")

        try:
            data = await self._get_balances(jwt_token)
            results = data.get("results") or []

            totals = defaultdict(Decimal)

            for item in results:
                token = (item.get("token") or "").strip().upper()
                size = self._d(item.get("size"))

                if token and size > 0:
                    totals[token] += size

            filtered_assets = {k: str(v) for k, v in totals.items() if v > 0}

            return BalanceResult(
                wallet=wallet,
                provider=self.name,
                totals=filtered_assets,
                details={
                    "assets": filtered_assets,
                },
            )

        except Exception as e:
            print(f"Error fetching Paradex balance: {e}")
            return BalanceResult(
                wallet=wallet,
                provider=self.name,
                totals={},
                details={"assets": {}},
            )

        finally:
            await self.aclose()