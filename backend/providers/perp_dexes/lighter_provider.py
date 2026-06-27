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
    # Lighter uses three creds for ZK-signing: account_index (numeric),
    # api_private_key (hex), api_key_index (default "255"). Read-only flows
    # don't need them; trading via the lighter-sdk CGO bridge does.
    needs_account_index = True
    needs_private_key = True
    needs_api_key_index = True
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

            # Lighter splits balance across three buckets per asset:
            #   balance         — free spot
            #   locked_balance  — locked in resting orders
            #   margin_balance  — posted as collateral for perpetuals
            # The old code summed only the first two, missing the perpetuals
            # equity entirely — a user with $20 spot + $21 perp margin saw
            # only $20 here while the Lighter UI showed $41 total.
            totals = defaultdict(Decimal)
            for asset in acc.get("assets") or []:
                symbol = (asset.get("symbol") or "").upper()
                if not symbol:
                    continue
                total = (
                    self._d(asset.get("balance"))
                    + self._d(asset.get("locked_balance"))
                    + self._d(asset.get("margin_balance"))
                )
                if total > 0:
                    totals[symbol] += total

            # Perpetuals are USDC-quoted on Lighter, so unrealized PnL on
            # open positions contributes to USDC equity. Realized PnL is
            # already folded into margin_balance / balance by the venue.
            unrealized = Decimal("0")
            for pos in acc.get("positions") or []:
                unrealized += self._d(pos.get("unrealized_pnl"))
            if unrealized != 0:
                totals["USDC"] += unrealized

            filtered_assets = {k: str(v) for k, v in totals.items() if v > 0}
            return BalanceResult(
                wallet=wallet,
                provider=self.name,
                totals=filtered_assets,
                details={
                    "assets": filtered_assets,
                    "total_asset_value": acc.get("total_asset_value"),
                    "collateral": acc.get("collateral"),
                    "available_balance": acc.get("available_balance"),
                    "unrealized_pnl_usd": str(unrealized),
                },
            )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                # Address not registered on Lighter — treat as empty balance
                return BalanceResult(wallet=wallet, provider=self.name, totals={}, details={"assets": {}})
            raise

        finally:
            await self.aclose()