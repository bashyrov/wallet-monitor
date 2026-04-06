import asyncio
import httpx
from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlencode

from backend.domain import ExchangeWallet
from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider
from settings import settings

from ._signing import ms, hex_hmac_sha256


class BybitProvider(BaseWalletProvider):
    name = "BybitProvider"
    label = "Bybit"
    enabled = True
    needs_passphrase = False
    base_url = settings.BYBIT_BASE_URL  # "https://api.bybit.com"

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=10)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers_get(self, wallet: ExchangeWallet, query_string: str, recv_window: str = "5000") -> dict[str, str]:
        ts = ms()
        sign_payload = f"{ts}{wallet.api_key}{recv_window}{query_string}"
        sign = hex_hmac_sha256(wallet.api_secret.strip(), sign_payload)

        return {
            "X-BAPI-API-KEY": wallet.api_key.strip(),
            "X-BAPI-SIGN": sign,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window,
        }

    async def _fetch_account(self, wallet: ExchangeWallet, account_type: str) -> defaultdict:
        params = {"accountType": account_type}
        qs = urlencode(params)
        r = await self._http.get(
            f"{self.base_url}/v5/account/wallet-balance?{qs}",
            headers=self._headers_get(wallet, qs),
        )
        r.raise_for_status()
        totals: defaultdict = defaultdict(Decimal)
        for acc in r.json().get("result", {}).get("list", []):
            for c in acc.get("coin", []):
                amt = Decimal(str(c.get("walletBalance") or "0"))
                if amt > 0:
                    totals[c["coin"]] += amt
        return totals

    async def fetch_balance(self, wallet: ExchangeWallet):
        results = await asyncio.gather(
            self._fetch_account(wallet, "UNIFIED"),
            self._fetch_account(wallet, "FUND"),
            return_exceptions=True,
        )

        trading_res, funding_res = results[0], results[1]

        if isinstance(trading_res, Exception) and isinstance(funding_res, Exception):
            raise trading_res  # both failed — propagate, service layer will show error

        trading = trading_res if isinstance(trading_res, defaultdict) else defaultdict(Decimal)
        funding = funding_res if isinstance(funding_res, defaultdict) else defaultdict(Decimal)

        # _build_result(wallet, provider, spot, futures, earn)
        # reuse spot=trading account, futures=funding account
        return self._build_result(wallet, self.name, dict(trading), dict(funding), {})