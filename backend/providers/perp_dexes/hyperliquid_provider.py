from collections import defaultdict
from decimal import Decimal
import asyncio

import requests

from backend.domain.models import BalanceResult
from backend.providers.base import BaseProvider


class HyperliquidProvider(BaseProvider):
    name = "HyperliquidProvider"
    base_url = "https://api.hyperliquid.xyz"

    def __init__(self) -> None:
        self._session = requests.Session()

    async def aclose(self) -> None:
        self._session.close()

    @staticmethod
    def _d(value) -> Decimal:
        if value in (None, "", False):
            return Decimal("0")
        return Decimal(str(value))

    def _post_info_sync(self, payload: dict) -> dict:
        url = f"{self.base_url}/info"
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }

        response = self._session.post(url, json=payload, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json()

    async def _post_info(self, payload: dict) -> dict:
        return await asyncio.to_thread(self._post_info_sync, payload)

    async def _get_perp_state(self, address: str) -> dict:
        payload = {
            "type": "clearinghouseState",
            "user": address,
        }
        return await self._post_info(payload)

    async def _get_spot_state(self, address: str) -> dict:
        payload = {
            "type": "spotClearinghouseState",
            "user": address,
        }
        return await self._post_info(payload)

    def _parse_spot_balances(self, data: dict) -> dict[str, Decimal]:
        totals = defaultdict(Decimal)

        balances = data.get("balances") or []
        for item in balances:
            coin = (item.get("coin") or "").strip().upper()
            total = self._d(item.get("total"))

            if coin and total > 0:
                totals[coin] += total

        return dict(totals)

    def _parse_perp_balances(self, data: dict) -> dict[str, Decimal]:
        """
        Для Hyperliquid perp самый полезный баланс — account value / withdrawable.
        Возвращаем его как USDC_PERP, только если > 0.
        """
        totals = defaultdict(Decimal)

        withdrawable = self._d(data.get("withdrawable"))
        if withdrawable > 0:
            totals["USDC_PERP"] += withdrawable
            return dict(totals)

        margin_summary = data.get("marginSummary") or {}
        account_value = self._d(margin_summary.get("accountValue"))
        if account_value > 0:
            totals["USDC_PERP"] += account_value

        return dict(totals)

    async def fetch_balance(self, wallet) -> BalanceResult:
        if not wallet.address:
            raise ValueError("Hyperliquid wallet requires address")

        try:
            perp_task = self._get_perp_state(wallet.address)
            spot_task = self._get_spot_state(wallet.address)

            perp_data, spot_data = await asyncio.gather(
                perp_task,
                spot_task,
                return_exceptions=True,
            )

            if isinstance(perp_data, Exception):
                print(f"Error fetching Hyperliquid perp state: {perp_data}")
                perp_data = {}

            if isinstance(spot_data, Exception):
                print(f"Error fetching Hyperliquid spot state: {spot_data}")
                spot_data = {}

            perp_balances = self._parse_perp_balances(perp_data)
            spot_balances = self._parse_spot_balances(spot_data)

            totals = defaultdict(Decimal)
            for bucket in (perp_balances, spot_balances):
                for asset, amount in bucket.items():
                    if amount > 0:
                        totals[asset] += amount

            filtered_assets = {k: str(v) for k, v in totals.items() if v > 0}

            return BalanceResult(
                wallet=wallet,
                provider=self.name,
                totals=filtered_assets,
                details={
                    "assets": filtered_assets,
                    "spot": {k: str(v) for k, v in spot_balances.items() if v > 0},
                    "perp": {k: str(v) for k, v in perp_balances.items() if v > 0},
                },
            )

        except Exception as e:
            print(f"Error fetching Hyperliquid balance: {e}")
            return BalanceResult(
                wallet=wallet,
                provider=self.name,
                totals={},
                details={"assets": {}},
            )

        finally:
            await self.aclose()