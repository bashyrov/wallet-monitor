import httpx
from collections import defaultdict
from decimal import Decimal

from backend.domain import ExchangeWallet
from backend.domain.models import BalanceResult
from backend.providers.base import BaseProvider
from settings import settings

from backend.providers.exchanges._signing import s, sha512_hex, hex_hmac_sha512


class GateProvider(BaseProvider):
    name = "GateProvider"
    # важно: без /api/v4
    base_url = settings.GATE_BASE_URL.rstrip("/")  # например "https://api.gateio.ws"

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=10)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self, wallet: ExchangeWallet, method: str, url_path: str, query: str = "", body: str = "") -> dict[str, str]:
        ts = s()
        payload_hash = sha512_hex(body or "")
        # ВАЖНО: url_path должен быть /api/v4/...
        sign_str = "\n".join([method.upper(), url_path, query, payload_hash, ts])
        sign = hex_hmac_sha512(wallet.api_secret.strip(), sign_str)

        return {
            "KEY": wallet.api_key.strip(),
            "SIGN": sign,
            "Timestamp": ts,
            "Content-Type": "application/json",
        }

    async def fetch_balance(self, wallet: ExchangeWallet) -> BalanceResult:
        try:
            url_path = "/api/v4/spot/accounts"
            headers = self._headers(wallet, "GET", url_path, query="", body="")

            r = await self._http.get(f"{self.base_url}{url_path}", headers=headers)

            if r.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"GATE error {r.status_code}: {r.text}",
                    request=r.request,
                    response=r,
                )

            data = r.json()
            totals = defaultdict(Decimal)

            for it in (data or []):
                cur = (it.get("currency") or "").upper()
                avail = it.get("available") or "0"
                locked = it.get("locked") or "0"
                amt = Decimal(str(avail)) + Decimal(str(locked))
                if cur and amt != 0:
                    totals[cur] += amt

            return BalanceResult(
                wallet=wallet,
                provider=self.name,
                totals={k: str(v) for k, v in totals.items() if v != 0},
                details={"spot": {k: str(v) for k, v in totals.items() if v != 0}},
            )
        finally:
            await self.aclose()