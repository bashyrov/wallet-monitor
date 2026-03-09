from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlencode
import asyncio

import requests

from backend.domain.models import BalanceResult
from backend.providers.base import BaseProvider


class LighterProvider(BaseProvider):
    name = "LighterProvider"
    base_url = "https://mainnet.zklighter.elliot.ai"

    def __init__(self) -> None:
        self._session = requests.Session()

    async def aclose(self) -> None:
        self._session.close()

    @staticmethod
    def _d(value) -> Decimal:
        if value in (None, "", False):
            return Decimal("0")
        return Decimal(str(value))

    def _get_account_sync(self, address: str) -> dict:
        params = {
            "by": "l1_address",
            "value": address,
        }
        url = f"{self.base_url}/api/v1/account?{urlencode(params)}"
        headers = {"accept": "application/json"}

        response = self._session.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()

    async def _get_account(self, address: str) -> dict:
        return await asyncio.to_thread(self._get_account_sync, address)

    @staticmethod
    def _extract_account(data: dict) -> dict:
        if not isinstance(data, dict):
            return {}

        # формат 1: аккаунт уже на верхнем уровне
        if "assets" in data:
            return data

        # формат 2: {"accounts": [{...}]}
        accounts = data.get("accounts") or []
        if accounts and isinstance(accounts[0], dict):
            return accounts[0]

        return {}

    async def fetch_balance(self, wallet) -> BalanceResult:
        if not wallet.address:
            raise ValueError("Lighter wallet requires l1_address")

        try:
            data = await self._get_account(wallet.address)
            acc = self._extract_account(data)

            assets = acc.get("assets") or []
            totals = defaultdict(Decimal)

            for asset in assets:
                symbol = (asset.get("symbol") or "").strip().upper()
                balance = self._d(asset.get("balance"))
                locked = self._d(asset.get("locked_balance"))
                total = balance + locked

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
            print(f"Error fetching Lighter balance: {e}")
            return BalanceResult(
                wallet=wallet,
                provider=self.name,
                totals={},
                details={"assets": {}},
            )

        finally:
            await self.aclose()