import httpx
from backend.providers.http import RetryClient
from collections import defaultdict
from decimal import Decimal
from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider


class EtherealProvider(BaseWalletProvider):
    name = "EtherealProvider"
    label = "Ethereal"
    enabled = True
    needs_api_key = False
    base_url = "https://api.ethereal.trade"

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
        address = getattr(wallet, "address", None) or getattr(wallet, "l1_address", None)
        if not address:
            raise ValueError("Ethereal wallet requires address or l1_address")

        try:
            resp = await self._client.get(f"{self.base_url}/v1/subaccount", params={"sender": address})
            resp.raise_for_status()
            subaccounts = resp.json().get("data") or []
            if not subaccounts:
                raise ValueError(f"No subaccounts found for {address}")
            subaccount_id = subaccounts[0].get("id")

            resp = await self._client.get(f"{self.base_url}/v1/subaccount/balance", params={"subaccountId": subaccount_id})
            resp.raise_for_status()
            items = resp.json().get("data") or []

            totals = defaultdict(Decimal)
            for item in items:
                symbol = (item.get("tokenName") or "").upper()
                available = self._d(item.get("available"))
                total_used = self._d(item.get("totalUsed"))
                amount = self._d(item.get("amount"))
                total = available + total_used or amount
                if symbol and total > 0:
                    totals[symbol] += total

            filtered_assets = {k: str(v) for k, v in totals.items() if v > 0}
            return BalanceResult(wallet=wallet, provider=self.name, totals=filtered_assets, details={"assets": filtered_assets})

        except Exception as e:
            print(f"Error fetching Ethereal balance: {e}")
            return BalanceResult(wallet=wallet, provider=self.name, totals={}, details={"assets": {}})

        finally:
            await self.aclose()