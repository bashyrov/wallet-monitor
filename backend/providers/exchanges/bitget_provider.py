import asyncio
import base64
import hashlib
import hmac
import json
from collections import defaultdict
from decimal import Decimal
from typing import Optional, Dict, Any
from urllib.parse import urlencode

import httpx

from backend.domain import ExchangeWallet
from backend.domain.models import BalanceResult
from backend.providers.base import BaseProvider
from settings import settings

from backend.providers.exchanges._signing import ms


class BitgetProvider(BaseProvider):
    name = "BitgetProvider"
    base_url = settings.BITGET_BASE_URL.rstrip("/")

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=20)

    async def aclose(self) -> None:
        await self._http.aclose()

    def creds_execution(self, wallet: ExchangeWallet) -> dict[str, str]:
        if not wallet.api_key or not wallet.api_secret or not wallet.api_passphrase:
            raise ValueError("BITGET api_key/api_secret/api_passphrase are required")

        return {
            "api_key": wallet.api_key.strip(),
            "api_secret": wallet.api_secret.strip(),
            "api_passphrase": wallet.api_passphrase.strip(),
        }

    def _sign(
        self,
        secret: str,
        timestamp: str,
        method: str,
        request_path: str,
        query_string: str = "",
        body: str = "",
    ) -> str:
        if query_string:
            message = f"{timestamp}{method.upper()}{request_path}?{query_string}{body}"
        else:
            message = f"{timestamp}{method.upper()}{request_path}{body}"

        digest = hmac.new(
            secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        return base64.b64encode(digest).decode("utf-8")

    def _headers(
        self,
        creds: dict[str, str],
        method: str,
        request_path: str,
        query_string: str = "",
        body: str = "",
    ) -> dict[str, str]:
        timestamp = str(int(ms()))
        sign = self._sign(
            secret=creds["api_secret"],
            timestamp=timestamp,
            method=method,
            request_path=request_path,
            query_string=query_string,
            body=body,
        )

        return {
            "ACCESS-KEY": creds["api_key"],
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": creds["api_passphrase"],
            "Content-Type": "application/json",
            "locale": "en-US",
        }

    async def _private_get(
        self,
        creds: dict[str, str],
        request_path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> dict:
        params = dict(params or {})
        query_string = urlencode(params, doseq=True)

        headers = self._headers(
            creds=creds,
            method="GET",
            request_path=request_path,
            query_string=query_string,
            body="",
        )

        url = f"{self.base_url}{request_path}"
        if query_string:
            url = f"{url}?{query_string}"

        r = await self._http.get(url, headers=headers)

        if r.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"BITGET error {r.status_code}: {r.text}",
                request=r.request,
                response=r,
            )

        data = r.json()

        # У Bitget успешный код обычно "00000"
        if str(data.get("code")) != "00000":
            raise ValueError(f"BITGET API error: {data}")

        return data

    async def get_spot_balance(self, creds: dict[str, str]) -> dict[str, Decimal]:
        totals = defaultdict(Decimal)

        data = await self._private_get(
            creds=creds,
            request_path="/api/v2/spot/account/assets",
            params={"assetType": "all"},
        )

        for item in data.get("data") or []:
            coin = (item.get("coin") or "").upper()
            available = Decimal(str(item.get("available") or "0"))
            frozen = Decimal(str(item.get("frozen") or "0"))
            locked = Decimal(str(item.get("locked") or "0"))
            limit_available = Decimal(str(item.get("limitAvailable") or "0"))

            total = available + frozen + locked + limit_available
            if coin and total != 0:
                totals[coin] += total

        return dict(totals)

    async def get_futures_balance_by_product_type(
        self,
        creds: dict[str, str],
        product_type: str,
    ) -> dict[str, Decimal]:
        totals = defaultdict(Decimal)

        data = await self._private_get(
            creds=creds,
            request_path="/api/v2/mix/account/accounts",
            params={"productType": product_type},
        )

        for account in data.get("data") or []:
            margin_coin = (account.get("marginCoin") or "").upper()

            # основной баланс аккаунта
            available = Decimal(str(account.get("available") or "0"))
            locked = Decimal(str(account.get("locked") or "0"))
            account_equity = Decimal(str(account.get("accountEquity") or "0"))

            # Если accountEquity есть, он обычно полезнее total balance,
            # потому что отражает equity аккаунта
            total = account_equity if account_equity != 0 else (available + locked)

            if margin_coin and total != 0:
                totals[margin_coin] += total

            # Иногда Bitget возвращает assetList с монетами внутри аккаунта
            for asset in account.get("assetList") or []:
                coin = (asset.get("coin") or "").upper()
                balance = Decimal(str(asset.get("balance") or "0"))
                if coin and balance != 0:
                    totals[coin] += balance

        return dict(totals)

    async def get_all_futures_balances(self, creds: dict[str, str]) -> dict[str, Decimal]:
        product_types = [
            "USDT-FUTURES",
            "COIN-FUTURES",
            "USDC-FUTURES",
        ]

        results = await asyncio.gather(
            *(self.get_futures_balance_by_product_type(creds, pt) for pt in product_types),
            return_exceptions=True,
        )

        totals = defaultdict(Decimal)

        for product_type, result in zip(product_types, results):
            if isinstance(result, Exception):
                print(f"Error fetching Bitget futures ({product_type}): {result}")
                continue

            for asset, amount in result.items():
                totals[asset] += amount

        return dict(totals)

    async def fetch_balance(self, wallet: ExchangeWallet) -> BalanceResult:
        creds = self.creds_execution(wallet)

        try:
            spot_task = self.get_spot_balance(creds)
            futures_task = self.get_all_futures_balances(creds)

            spot, futures = await asyncio.gather(
                spot_task,
                futures_task,
                return_exceptions=True,
            )

            if isinstance(spot, Exception):
                raise spot

            if isinstance(futures, Exception):
                print(f"Error fetching Bitget futures: {futures}")
                futures = {}

            totals = defaultdict(Decimal)
            for bucket in (spot, futures):
                for asset, amount in bucket.items():
                    totals[asset] += amount

            return BalanceResult(
                wallet=wallet,
                provider=self.name,
                totals={k: str(v) for k, v in totals.items() if v != 0},
                details={
                    "spot": {k: str(v) for k, v in spot.items() if v != 0},
                    "futures": {k: str(v) for k, v in futures.items() if v != 0},
                },
            )

        except Exception as e:
            print(f"Error fetching Bitget balance: {e}")
            return BalanceResult(wallet=wallet, provider=self.name)

        finally:
            await self.aclose()