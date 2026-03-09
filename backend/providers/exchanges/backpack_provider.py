import asyncio
import base64
import time
from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.domain import ExchangeWallet
from backend.domain.models import BalanceResult
from backend.providers.base import BaseProvider


class BackpackProvider(BaseProvider):
    name = "BackpackProvider"
    base_url = "https://api.backpack.exchange"
    recv_window = 60000

    def __init__(self) -> None:
        self._session = requests.Session()

    async def aclose(self) -> None:
        self._session.close()

    @staticmethod
    def _d(value) -> Decimal:
        if value in (None, "", False):
            return Decimal("0")
        return Decimal(str(value))

    @staticmethod
    def _build_signing_string(
        instruction: str,
        timestamp: int,
        window: int,
        params: dict | None = None,
    ) -> str:
        parts: list[tuple[str, str]] = [("instruction", instruction)]

        if params:
            for key, value in sorted(params.items()):
                if value is not None:
                    parts.append((key, str(value)))

        parts.append(("timestamp", str(timestamp)))
        parts.append(("window", str(window)))

        return urlencode(parts)

    @staticmethod
    def _sign(message: str, private_key_b64: str) -> str:
        seed = base64.b64decode(private_key_b64)
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
        signature = private_key.sign(message.encode("utf-8"))
        return base64.b64encode(signature).decode("utf-8")

    def _signed_get_sync(
        self,
        path: str,
        instruction: str,
        api_key: str,
        api_secret: str,
        params: dict | None = None,
    ) -> dict | list:
        timestamp = int(time.time() * 1000)
        window = self.recv_window

        signing_string = self._build_signing_string(
            instruction=instruction,
            timestamp=timestamp,
            window=window,
            params=params,
        )
        signature = self._sign(signing_string, api_secret)

        url = f"{self.base_url}{path}"
        headers = {
            "Accept": "application/json",
            "X-API-KEY": api_key,
            "X-SIGNATURE": signature,
            "X-TIMESTAMP": str(timestamp),
            "X-WINDOW": str(window),
        }

        response = self._session.get(
            url,
            headers=headers,
            params=params,
            timeout=20,
        )

        if response.status_code >= 400:
            raise requests.HTTPError(
                (
                    f"BACKPACK error {response.status_code}: {response.text}\n"
                    f"url={response.url}\n"
                    f"signing_string={signing_string}"
                ),
                response=response,
            )

        return response.json()

    async def _signed_get(
        self,
        path: str,
        instruction: str,
        api_key: str,
        api_secret: str,
        params: dict | None = None,
    ) -> dict | list:
        return await asyncio.to_thread(
            self._signed_get_sync,
            path,
            instruction,
            api_key,
            api_secret,
            params,
        )

    async def _get_spot_balances(
        self,
        api_key: str,
        api_secret: str,
    ) -> dict[str, Decimal]:
        data = await self._signed_get(
            path="/api/v1/capital",
            instruction="balanceQuery",
            api_key=api_key,
            api_secret=api_secret,
        )

        totals = defaultdict(Decimal)

        if isinstance(data, dict):
            for symbol, info in data.items():
                if not isinstance(info, dict):
                    continue

                asset = (symbol or "").strip().upper()
                available = self._d(info.get("available"))
                locked = self._d(info.get("locked"))
                staked = self._d(info.get("staked"))

                total = available + locked + staked
                if asset and total > 0:
                    totals[asset] += total

        return dict(totals)

    async def _get_futures_collateral(
        self,
        api_key: str,
        api_secret: str,
    ) -> dict[str, Decimal]:
        data = await self._signed_get(
            path="/api/v1/capital/collateral",
            instruction="collateralQuery",
            api_key=api_key,
            api_secret=api_secret,
        )

        totals = defaultdict(Decimal)

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("results") or data.get("collateral") or []
            if not items:
                for symbol, info in data.items():
                    if isinstance(info, dict):
                        items.append({"symbol": symbol, **info})
        else:
            items = []

        for item in items:
            symbol = (
                item.get("symbol")
                or item.get("asset")
                or item.get("token")
                or item.get("currency")
                or ""
            ).strip().upper()

            total = (
                self._d(item.get("available"))
                + self._d(item.get("locked"))
                + self._d(item.get("collateral"))
                + self._d(item.get("balance"))
            )

            if total == 0:
                total = self._d(item.get("amount")) + self._d(item.get("size"))

            if symbol and total > 0:
                totals[symbol] += total

        return dict(totals)

    async def fetch_balance(self, wallet: ExchangeWallet) -> BalanceResult:
        if not wallet.api_key or not wallet.api_secret:
            raise ValueError("Backpack wallet requires api_key and api_secret")

        api_key = wallet.api_key.strip()
        api_secret = wallet.api_secret.strip()

        try:
            spot_task = self._get_spot_balances(api_key, api_secret)
            collateral_task = self._get_futures_collateral(api_key, api_secret)

            spot, futures_collateral = await asyncio.gather(
                spot_task,
                collateral_task,
                return_exceptions=True,
            )

            if isinstance(spot, Exception):
                print(f"Error fetching Backpack spot balances: {spot}")
                spot = {}

            if isinstance(futures_collateral, Exception):
                print(f"Error fetching Backpack futures collateral: {futures_collateral}")
                futures_collateral = {}

            totals = defaultdict(Decimal)

            for bucket in (spot, futures_collateral):
                for asset, amount in bucket.items():
                    if amount > 0:
                        totals[asset] += amount

            filtered_totals = {k: str(v) for k, v in totals.items() if v > 0}
            filtered_spot = {k: str(v) for k, v in spot.items() if v > 0}
            filtered_futures_collateral = {
                k: str(v) for k, v in futures_collateral.items() if v > 0
            }

            return BalanceResult(
                wallet=wallet,
                provider=self.name,
                totals=filtered_totals,
                details={
                    "assets": filtered_totals,
                    "spot": filtered_spot,
                    "futures_collateral": filtered_futures_collateral,
                },
            )

        except Exception as e:
            print(f"Error fetching Backpack balance: {e}")
            return BalanceResult(
                wallet=wallet,
                provider=self.name,
                totals={},
                details={
                    "assets": {},
                    "spot": {},
                    "futures_collateral": {},
                },
            )

        finally:
            await self.aclose()