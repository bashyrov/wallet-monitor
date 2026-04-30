import asyncio
import logging
from collections import defaultdict
from decimal import Decimal
from typing import Optional, Dict, Any
from urllib.parse import urlencode

import httpx
from backend.providers.http import RetryClient
from binance.async_client import AsyncClient

from backend.domain import ExchangeWallet
from backend.providers.base_wallet_provider import BaseWalletProvider

logger = logging.getLogger("avalant.providers.binance")
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

    async def _signed_sapi_post(
        self,
        creds: dict[str, str],
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Same signing as _signed_sapi_get but POST. Used by funding-wallet
        and a few other endpoints that strictly require POST."""
        params = dict(params or {})
        params["timestamp"] = int(ms())
        params.setdefault("recvWindow", 5000)

        qs = urlencode(params, doseq=True)
        sig = hex_hmac_sha256(creds["api_secret"], qs)

        url = f"{self.base_url}{path}?{qs}&signature={sig}"
        headers = {"X-MBX-APIKEY": creds["api_key"]}

        r = await self._http.post(url, headers=headers)
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

    async def get_futures_balance(self) -> tuple[dict[str, Decimal], str]:
        totals = defaultdict(Decimal)
        futures_account = await self.client.futures_account()
        for asset in futures_account["assets"]:
            total = Decimal(asset["walletBalance"])
            if total > 0:
                totals[asset["asset"]] = total
        upnl = str(futures_account.get("totalUnrealizedProfit") or "0")
        return dict(totals), upnl

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

    async def get_funding_wallet(self, creds: dict[str, str]) -> dict[str, Decimal]:
        """Funding wallet — separate bucket for fiat conversions, P2P,
        copy-trade balance, etc. Endpoint POSTs but is signed exactly like
        the GETs, just a different verb."""
        try:
            data = await self._signed_sapi_post(creds, "/sapi/v1/asset/get-funding-asset", {})
        except Exception as exc:
            logger.warning("Binance funding-wallet fetch failed: %s", exc)
            return {}
        out = defaultdict(Decimal)
        for it in (data or []):
            asset = (it.get("asset") or "").strip()
            free = Decimal(str(it.get("free") or "0"))
            locked = Decimal(str(it.get("locked") or "0"))
            freeze = Decimal(str(it.get("freeze") or "0"))
            total = free + locked + freeze
            if asset and total != 0:
                out[asset] += total
        return dict(out)

    async def get_cross_margin_balances(self, creds: dict[str, str]) -> dict[str, Decimal]:
        """Cross-margin account. Some users keep balances here for
        leveraged spot trades. Endpoint: /sapi/v1/margin/account."""
        try:
            data = await self._signed_sapi_get(creds, "/sapi/v1/margin/account", {})
        except Exception as exc:
            logger.warning("Binance cross-margin fetch failed: %s", exc)
            return {}
        out = defaultdict(Decimal)
        for asset_row in (data.get("userAssets") or []):
            asset = (asset_row.get("asset") or "").strip()
            net = Decimal(str(asset_row.get("netAsset") or "0"))
            if asset and net != 0:
                out[asset] += net
        return dict(out)

    async def fetch_balance(self, wallet: ExchangeWallet):
        creds = self.creds_execution(wallet)
        self.client = await AsyncClient.create(creds["api_key"], creds["api_secret"])

        try:
            # Pull every Binance bucket in parallel. Each can fail
            # independently (e.g. user disabled futures permission on the
            # API key) — we log and continue rather than dropping the
            # whole portfolio fetch.
            spot, futures, earn, funding, cross_margin = await asyncio.gather(
                self.get_spot_balance(),
                self.get_futures_balance(),
                self.get_simple_earn_balances(creds),
                self.get_funding_wallet(creds),
                self.get_cross_margin_balances(creds),
                return_exceptions=True,
            )

            if isinstance(spot, Exception):
                logger.warning("Binance spot fetch failed: %s", spot)
                raise spot
            if isinstance(futures, Exception):
                logger.warning("Binance futures fetch failed: %s", futures)
                futures, upnl = {}, None
            else:
                futures, upnl = futures
            if isinstance(earn, Exception):
                logger.warning("Binance earn fetch failed: %s", earn)
                earn = {}
            if isinstance(funding, Exception):
                logger.warning("Binance funding fetch failed: %s", funding)
                funding = {}
            if isinstance(cross_margin, Exception):
                logger.warning("Binance cross-margin fetch failed: %s", cross_margin)
                cross_margin = {}

            # Merge funding + cross-margin into the spot bucket (these are
            # all "spendable" classifications, not earn-locked).
            merged_spot = defaultdict(Decimal, spot)
            for asset, amt in funding.items():
                merged_spot[asset] += amt
            for asset, amt in cross_margin.items():
                merged_spot[asset] += amt

            return self._build_result(wallet, self.name, dict(merged_spot), futures, earn, upnl_usd=upnl)

        finally:
            await self.client.close_connection()
            await self.aclose()