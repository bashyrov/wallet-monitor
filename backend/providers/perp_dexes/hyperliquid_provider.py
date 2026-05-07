import httpx
from backend.providers.http import RetryClient
from collections import defaultdict
from decimal import Decimal
from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider
import asyncio


class HyperliquidProvider(BaseWalletProvider):
    name = "HyperliquidProvider"
    label = "Hyperliquid"
    enabled = True
    needs_api_key = False
    # EVM private key for EIP-712 phantom-agent signing on /exchange.
    # Read-only paths (positions, balance) work without it; trading does not.
    needs_private_key = True
    base_url = "https://api.hyperliquid.xyz"

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
        if not wallet.address:
            raise ValueError("Hyperliquid wallet requires address")

        try:
            async def post(payload):
                resp = await self._client.post(f"{self.base_url}/info", json=payload, headers={"accept": "application/json"})
                resp.raise_for_status()
                return resp.json()

            perp_payload = {"type": "clearinghouseState", "user": wallet.address}
            spot_payload = {"type": "spotClearinghouseState", "user": wallet.address}

            perp_data, spot_data = await asyncio.gather(post(perp_payload), post(spot_payload), return_exceptions=True)

            totals = defaultdict(Decimal)

            if isinstance(perp_data, dict):
                val = self._d(perp_data.get("withdrawable") or 0)
                if val == 0:
                    val = self._d((perp_data.get("marginSummary") or {}).get("accountValue") or 0)
                if val > 0:
                    totals["USDC_PERP"] += val

            if isinstance(spot_data, dict):
                for item in spot_data.get("balances") or []:
                    coin = (item.get("coin") or "").upper()
                    amt = self._d(item.get("total"))
                    if coin and amt > 0:
                        totals[coin] += amt

            filtered_assets = {k: str(v) for k, v in totals.items() if v > 0}
            return BalanceResult(wallet=wallet, provider=self.name, totals=filtered_assets, details={"assets": filtered_assets})

        except Exception as e:
            print(f"Error fetching Hyperliquid balance: {e}")
            return BalanceResult(wallet=wallet, provider=self.name, totals={}, details={"assets": {}})

        finally:
            await self.aclose()