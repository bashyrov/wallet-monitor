import time

import httpx
from collections import defaultdict
from decimal import Decimal

from backend.domain import ExchangeWallet
from backend.domain.models import BalanceResult
from backend.providers.base import BaseProvider
from settings import settings

from ._signing import ms, b64_hmac_sha256


class KucoinProvider(BaseProvider):
    name = "KucoinProvider"
    base_url = settings.KUCOIN_BASE_URL  # "https://api.kucoin.com"
    key_version = "2"

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=10)
        self._ts_cached: str | None = None
        self._ts_cached_at: float = 0.0
        self._ts_ttl_s: float = 25.0

    async def _server_ts_ms(self) -> str:
        now = time.time()
        if self._ts_cached and (now - self._ts_cached_at) < self._ts_ttl_s:
            return self._ts_cached

        # KuCoin: GET /api/v1/timestamp -> {"code":"200000","data":171...}
        r = await self._http.get(f"{self.base_url}/api/v1/timestamp")
        r.raise_for_status()
        data = r.json()
        ts = str(int(data["data"]))  # ms

        self._ts_cached = ts
        self._ts_cached_at = now
        return ts

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _headers(self, wallet: ExchangeWallet, method: str, path: str, body: str = "") -> dict[str, str]:
        if not wallet.api_passphrase:
            raise ValueError("KuCoin requires api_passphrase")

        ts = await self._server_ts_ms()
        prehash = f"{ts}{method.upper()}{path}{body}"
        sign = b64_hmac_sha256(wallet.api_secret.strip(), prehash)

        passphrase = wallet.api_passphrase.strip()
        if self.key_version == "2":
            passphrase = b64_hmac_sha256(wallet.api_secret.strip(), passphrase)

        return {
            "KC-API-KEY": wallet.api_key.strip(),
            "KC-API-SIGN": sign,
            "KC-API-TIMESTAMP": ts,
            "KC-API-PASSPHRASE": passphrase,
            "KC-API-KEY-VERSION": self.key_version,
            "Content-Type": "application/json",
        }

    async def fetch_balance(self, wallet: ExchangeWallet) -> BalanceResult:
        # /api/v1/accounts — один запрос, вернёт balances по типам account
        path = "/api/v1/accounts"
        headers = await self._headers(wallet, "GET", path)

        r = await self._http.get(f"{self.base_url}{path}", headers=headers)
        r.raise_for_status()
        data = r.json()

        totals = defaultdict(Decimal)
        for it in (data.get("data") or []):
            cur = it.get("currency")
            bal = it.get("balance") or "0"
            amt = Decimal(str(bal))
            if cur and amt != 0:
                totals[cur] += amt

        return BalanceResult(
            wallet=wallet,
            provider=self.name,
            totals={k: str(v) for k, v in totals.items() if v != 0},
            details={"accounts": {k: str(v) for k, v in totals.items() if v != 0}},
        )