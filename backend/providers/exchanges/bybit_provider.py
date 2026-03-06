import httpx
from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlencode

from backend.domain import ExchangeWallet
from backend.domain.models import BalanceResult
from backend.providers.base import BaseProvider
from settings import settings

from ._signing import ms, hex_hmac_sha256


class BybitProvider(BaseProvider):
    name = "BybitProvider"
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

    async def fetch_balance(self, wallet: ExchangeWallet) -> BalanceResult:
        # unified / spot баланс: /v5/account/wallet-balance
        params = {"accountType": "UNIFIED"}
        qs = urlencode(params)
        url = f"{self.base_url}/v5/account/wallet-balance?{qs}"

        headers = self._headers_get(wallet, qs)
        r = await self._http.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()

        totals = defaultdict(Decimal)
        for acc in ((data.get("result") or {}).get("list") or []):
            for coin in (acc.get("coin") or []):
                sym = coin.get("coin")
                bal = coin.get("walletBalance") or coin.get("equity") or "0"
                amt = Decimal(str(bal))
                if sym and amt != 0:
                    totals[sym] += amt

        return BalanceResult(
            wallet=wallet,
            provider=self.name,
            totals={k: str(v) for k, v in totals.items() if v != 0},
            details={"wallet": {k: str(v) for k, v in totals.items() if v != 0}},
        )