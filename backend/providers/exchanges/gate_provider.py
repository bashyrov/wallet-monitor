import asyncio
from backend.providers.http import RetryClient
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
        self._http = RetryClient(timeout=10)

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

    async def _get_spot(self, wallet: ExchangeWallet) -> dict[str, Decimal]:
        path = "/api/v4/spot/accounts"
        r = await self._http.get(
            f"{self.base_url}{path}",
            headers=self._headers(wallet, "GET", path),
        )
        r.raise_for_status()
        out = defaultdict(Decimal)
        for x in r.json():
            total = Decimal(x["available"]) + Decimal(x["locked"])
            if total > 0:
                out[x["currency"].upper()] += total
        return dict(out)

    async def _get_futures(self, wallet: ExchangeWallet, settle: str) -> dict[str, Decimal]:
        """settle = 'usdt' or 'btc'"""
        path = f"/api/v4/futures/{settle}/accounts"
        r = await self._http.get(
            f"{self.base_url}{path}",
            headers=self._headers(wallet, "GET", path),
        )
        r.raise_for_status()
        data = r.json()
        currency = (data.get("currency") or settle).upper()
        total = Decimal(str(data.get("total") or "0"))
        return {currency: total} if total > 0 else {}

    async def _get_earn(self, wallet: ExchangeWallet) -> dict[str, Decimal]:
        """Gate.io Flexible Earn (Uni holdings)"""
        path = "/api/v4/earn/uni/holdings"
        try:
            r = await self._http.get(
                f"{self.base_url}{path}",
                headers=self._headers(wallet, "GET", path),
            )
            r.raise_for_status()
            out = defaultdict(Decimal)
            for item in r.json():
                currency = (item.get("currency") or "").upper()
                amount = Decimal(str(item.get("current_amount") or "0"))
                if currency and amount > 0:
                    out[currency] += amount
            return dict(out)
        except Exception:
            return {}

    async def fetch_balance(self, wallet: ExchangeWallet):
        spot, fut_usdt, fut_btc, earn = await asyncio.gather(
            self._get_spot(wallet),
            self._get_futures(wallet, "usdt"),
            self._get_futures(wallet, "btc"),
            self._get_earn(wallet),
            return_exceptions=True,
        )

        if isinstance(spot, Exception):
            raise spot

        futures: dict[str, Decimal] = defaultdict(Decimal)
        for f in (fut_usdt, fut_btc):
            if not isinstance(f, Exception):
                for k, v in f.items():
                    futures[k] += v

        earn_dict = earn if not isinstance(earn, Exception) else {}

        return self._build_result(wallet, self.name, dict(spot), dict(futures), earn_dict)