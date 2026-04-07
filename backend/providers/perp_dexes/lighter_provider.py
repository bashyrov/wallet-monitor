from urllib.parse import urlencode
import httpx
from backend.providers.http import RetryClient

from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider
from collections import defaultdict
from decimal import Decimal


class LighterProvider(BaseWalletProvider):
    name = "LighterProvider"
    label = "Lighter"
    enabled = True
    needs_api_key = False
    base_url = "https://mainnet.zklighter.elliot.ai"

    def __init__(self):
        self._client = RetryClient(timeout=15.0)

    async def aclose(self):
        await self._client.aclose()

    @staticmethod
    def _d(value):
        if value in (None, "", False):
            return Decimal("0")
        return Decimal(str(value))

    async def fetch_balance(self, wallet) -> BalanceResult:
        if not wallet.address:
            raise ValueError("Lighter wallet requires l1_address")
        try:
            params = {"by": "l1_address", "value": wallet.address}
            url = f"{self.base_url}/api/v1/account?{urlencode(params)}"
            resp = await self._client.get(url, headers={"accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()

            acc = data if "assets" in data else (data.get("accounts") or [{}])[0]

            totals = defaultdict(Decimal)
            for asset in acc.get("assets") or []:
                symbol = (asset.get("symbol") or "").upper()
                total = self._d(asset.get("balance")) + self._d(asset.get("locked_balance"))
                if symbol and total > 0:
                    totals[symbol] += total

            filtered_assets = {k: str(v) for k, v in totals.items() if v > 0}
            return BalanceResult(wallet=wallet, provider=self.name, totals=filtered_assets, details={"assets": filtered_assets})

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                # Address not registered on Lighter — treat as empty balance
                return BalanceResult(wallet=wallet, provider=self.name, totals={}, details={"assets": {}})
            raise

        finally:
            await self.aclose()