import asyncio
import base64
import time
from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.domain import ExchangeWallet
from backend.providers.base_wallet_provider import BaseWalletProvider


class BackpackProvider(BaseWalletProvider):
    name = "BackpackProvider"
    label = "Backpack"
    enabled = True
    needs_passphrase = False
    base_url = "https://api.backpack.exchange"
    recv_window = 60000

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=20)

    async def aclose(self):
        await self._http.aclose()

    @staticmethod
    def _d(v):
        return Decimal(str(v or "0"))

    def _sign(self, msg: str, secret: str) -> str:
        seed = base64.b64decode(secret)
        pk = Ed25519PrivateKey.from_private_bytes(seed)
        sig = pk.sign(msg.encode())
        return base64.b64encode(sig).decode()

    def _build_string(self, instruction, ts, params=None):
        parts = [("instruction", instruction)]
        if params:
            parts += sorted((k, str(v)) for k, v in params.items())
        parts += [("timestamp", str(ts)), ("window", str(self.recv_window))]
        return urlencode(parts)

    async def _get(self, path, instruction, key, secret):
        ts = int(time.time() * 1000)
        sign_str = self._build_string(instruction, ts)
        sig = self._sign(sign_str, secret)

        r = await self._http.get(
            f"{self.base_url}{path}",
            headers={
                "X-API-KEY": key,
                "X-SIGNATURE": sig,
                "X-TIMESTAMP": str(ts),
                "X-WINDOW": str(self.recv_window),
            },
        )
        r.raise_for_status()
        return r.json()

    async def _get_spot(self, key, secret):
        data = await self._get("/api/v1/capital", "balanceQuery", key, secret)
        out = defaultdict(Decimal)

        for sym, v in data.items():
            total = self._d(v.get("available")) + self._d(v.get("locked"))
            if total > 0:
                out[sym.upper()] += total

        return dict(out)

    async def _get_futures(self, key, secret):
        data = await self._get("/api/v1/capital/collateral", "collateralQuery", key, secret)
        out = defaultdict(Decimal)

        for x in data if isinstance(data, list) else data.values():
            sym = (x.get("symbol") or "").upper()
            total = self._d(x.get("available")) + self._d(x.get("locked"))
            if sym and total > 0:
                out[sym] += total

        return dict(out)

    async def fetch_balance(self, wallet: ExchangeWallet):
        key, secret = wallet.api_key.strip(), wallet.api_secret.strip()

        spot, futures = await asyncio.gather(
            self._get_spot(key, secret),
            self._get_futures(key, secret),
            return_exceptions=True,
        )

        if isinstance(spot, Exception): spot = {}
        if isinstance(futures, Exception): futures = {}

        return self._build_result(wallet, self.name, spot, futures, {})