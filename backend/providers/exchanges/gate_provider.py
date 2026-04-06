import httpx
from collections import defaultdict
from decimal import Decimal

from backend.domain import ExchangeWallet
from backend.providers.base_wallet_provider import BaseWalletProvider
from settings import settings

from backend.providers.exchanges._signing import s, sha512_hex, hex_hmac_sha512


class GateProvider(BaseWalletProvider):
    name = "GateProvider"
    label = "Gate.io"
    enabled = True
    needs_passphrase = False
    # важно: без /api/v4
    base_url = settings.GATE_BASE_URL.rstrip("/")  # например "https://api.gateio.ws"

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=10)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self, wallet: ExchangeWallet, method: str, url_path: str, query: str = "", body: str = "") -> dict[str, str]:
        ts = s()
        payload_hash = sha512_hex(body or "")
        sign_str = "\n".join([method.upper(), url_path, query, payload_hash, ts])
        sign = hex_hmac_sha512(wallet.api_secret.strip(), sign_str)

        return {
            "KEY": wallet.api_key.strip(),
            "SIGN": sign,
            "Timestamp": ts,
            "Content-Type": "application/json",
        }

    async def fetch_balance(self, wallet: ExchangeWallet):
        path = "/api/v4/spot/accounts"
        r = await self._http.get(
            f"{self.base_url}{path}",
            headers=self._headers(wallet, "GET", path),
        )
        r.raise_for_status()

        spot = defaultdict(Decimal)

        for x in r.json():
            total = Decimal(x["available"]) + Decimal(x["locked"])
            if total > 0:
                spot[x["currency"].upper()] += total

        return self._build_result(wallet, self.name, dict(spot), {}, {})