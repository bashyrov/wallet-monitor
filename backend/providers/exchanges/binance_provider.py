import asyncio
from collections import defaultdict
from decimal import Decimal
from typing import Optional, Dict, Any
from urllib.parse import urlencode

import httpx
from backend.providers.http import RetryClient
from binance.async_client import AsyncClient

from backend.domain import ExchangeWallet
from backend.providers.base_wallet_provider import BaseWalletProvider
from settings import settings

from backend.providers.exchanges._signing import ms, hex_hmac_sha256


class BinanceProvider(BaseWalletProvider):
    name = "BinanceProvider"
    label = "Binance"
    enabled = True
    needs_passphrase = False
    base_url = settings.BINANCE_BASE_URL

    def __init__(self) -> None:
        self._http = RetryClient(timeout=20)
        self.client: AsyncClient | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    def creds_execution(self, wallet: ExchangeWallet) -> dict[str, str]:
        if not wallet.api_key or not wallet.api_secret:
            raise ValueError("BINANCE api_key/api_secret are required")
        return {
            "api_key": wallet.api_key.strip(),
            "api_secret": wallet.api_secret.strip(),
        }

    async def _signed_sapi_get(
        self,
        creds: dict[str, str],
        path: str,
        params: Optional[Dict[str, Any]] = None
    ) -> dict:
        params = dict(params or {})
        params["timestamp"] = int(ms())
        params.setdefault("recvWindow", 5000)

        qs = urlencode(params, doseq=True)
        sig = hex_hmac_sha256(creds["api_secret"], qs)

        url = f"{self.base_url}{path}?{qs}&signature={sig}"
        headers = {"X-MBX-APIKEY": creds["api_key"]}

        r = await self._http.get(url, headers=headers)
        r.raise_for_status()
        return r.json()

    async def get_spot_balance(self) -> dict[str, Decimal]:
        totals = defaultdict(Decimal)
        spot_account = await self.client.get_account()
        for asset in spot_account["balances"]:
            total = Decimal(asset["free"]) + Decimal(asset["locked"])
            if total > 0:
                totals[asset["asset"]] = total
        return dict(totals)

    async def get_futures_balance(self) -> dict[str, Decimal]:
        totals = defaultdict(Decimal)
        futures_account = await self.client.futures_account()
        for asset in futures_account["assets"]:
            total = Decimal(asset["walletBalance"])
            if total > 0:
                totals[asset["asset"]] = total
        return dict(totals)

    async def get_simple_earn_balances(self, creds: dict[str, str]) -> dict[str, Decimal]:
        totals = defaultdict(Decimal)

        flex = await self._signed_sapi_get(creds, "/sapi/v1/simple-earn/flexible/position", {"size": 100})
        for it in (flex.get("rows") or []):
            asset = (it.get("asset") or "").strip()
            amt = Decimal(str(it.get("totalAmount") or "0"))
            if asset and amt != 0:
                totals[asset] += amt

        locked = await self._signed_sapi_get(creds, "/sapi/v1/simple-earn/locked/position", {"size": 100})
        for it in (locked.get("rows") or []):
            asset = (it.get("asset") or "").strip()
            amt = Decimal(str(it.get("amount") or "0"))
            if asset and amt != 0:
                totals[asset] += amt

        return {k: v for k, v in totals.items() if v != 0}

    async def fetch_balance(self, wallet: ExchangeWallet):
        creds = self.creds_execution(wallet)
        self.client = await AsyncClient.create(creds["api_key"], creds["api_secret"])

        try:
            spot, futures, earn = await asyncio.gather(
                self.get_spot_balance(),
                self.get_futures_balance(),
                self.get_simple_earn_balances(creds),
                return_exceptions=True,
            )

            if isinstance(spot, Exception): raise spot
            if isinstance(futures, Exception): futures = {}
            if isinstance(earn, Exception): earn = {}

            return self._build_result(wallet, self.name, spot, futures, earn)

        finally:
            await self.client.close_connection()
            await self.aclose()