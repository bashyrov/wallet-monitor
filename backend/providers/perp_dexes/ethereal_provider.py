from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlencode
import asyncio

import requests

from backend.domain.models import BalanceResult
from backend.providers.base import BaseProvider


class EtherealProvider(BaseProvider):
    name = "EtherealProvider"
    base_url = "https://api.ethereal.trade"

    def __init__(self) -> None:
        self._session = requests.Session()

    async def aclose(self) -> None:
        self._session.close()

    @staticmethod
    def _d(value) -> Decimal:
        if value in (None, "", False):
            return Decimal("0")
        return Decimal(str(value))

    def _get_sync(self, path: str, params: dict | None = None) -> dict:
        query = urlencode(params or {})
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        response = self._session.get(
            url,
            headers={"accept": "application/json"},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        return await asyncio.to_thread(self._get_sync, path, params)

    async def _get_subaccount_id(self, address: str) -> str:
        data = await self._get("/v1/subaccount", {"sender": address})
        items = data.get("data") or []

        if not items:
            raise ValueError(f"Ethereal: no subaccounts found for address {address}")

        subaccount_id = items[0].get("id")
        if not subaccount_id:
            raise ValueError(f"Ethereal: subaccount id not found for address {address}")

        return subaccount_id

    async def _get_balances(self, subaccount_id: str) -> dict:
        return await self._get(
            "/v1/subaccount/balance",
            {"subaccountId": subaccount_id},
        )

    async def fetch_balance(self, wallet) -> BalanceResult:
        address = getattr(wallet, "address", None) or getattr(wallet, "l1_address", None)
        if not address:
            raise ValueError("Ethereal wallet requires address or l1_address")

        try:
            subaccount_id = await self._get_subaccount_id(address)
            data = await self._get_balances(subaccount_id)

            items = data.get("data") or []
            totals = defaultdict(Decimal)

            for item in items:
                symbol = (item.get("tokenName") or "").strip().upper()

                # В docs есть amount / available / totalUsed.
                # Для итогового баланса берем available + totalUsed,
                # а если они пустые — fallback на amount.
                available = self._d(item.get("available"))
                total_used = self._d(item.get("totalUsed"))
                amount = self._d(item.get("amount"))

                total = available + total_used
                if total == 0:
                    total = amount

                if symbol and total > 0:
                    totals[symbol] += total

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
            print(f"Error fetching Ethereal balance: {e}")
            return BalanceResult(
                wallet=wallet,
                provider=self.name,
                totals={},
                details={"assets": {}},
            )

        finally:
            await self.aclose()