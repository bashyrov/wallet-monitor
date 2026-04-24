"""Ourbit spot wallet provider.

Ourbit (https://ourbit.com) exposes a Binance-style v3 REST API:
  · GET /api/v3/time           — server timestamp for signed requests
  · GET /api/v3/account        — spot balances (signed)
  · GET /api/v3/myTrades       — per-symbol fill history (signed)
  · GET /api/v3/capital/deposit/hisrec  — deposit history (signed)
  · GET /api/v3/capital/withdraw/history — withdraw history (signed)

Spot-only exchange (no /fapi). Signing is HMAC-SHA256 over the URL-encoded
query string, signature appended as `&signature=<hex>`, API key sent via
`X-MEXC-APIKEY`-style header. We tested and confirmed Ourbit accepts
`X-MBX-APIKEY` (the same header Binance uses).
"""
from collections import defaultdict
from decimal import Decimal
import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from backend.providers.http import RetryClient

from backend.domain import ExchangeWallet
from backend.providers.base_wallet_provider import BaseWalletProvider
from settings import settings

from backend.providers.exchanges._signing import ms, hex_hmac_sha256


class OurbitProvider(BaseWalletProvider):
    name = "OurbitProvider"
    label = "Ourbit"
    enabled = True
    needs_passphrase = False
    base_url = settings.OURBIT_BASE_URL

    RECV_WINDOW = 5000

    def __init__(self) -> None:
        self._http = RetryClient(timeout=15)
        self._ts_cached: int | None = None
        self._ts_cached_at: float = 0.0
        self._ts_ttl_s: float = 25.0

    async def aclose(self) -> None:
        await self._http.aclose()

    def _creds(self, wallet: ExchangeWallet) -> dict[str, str]:
        if not wallet.api_key or not wallet.api_secret:
            raise ValueError("Ourbit: api_key/api_secret are required")
        return {
            "api_key": wallet.api_key.strip(),
            "api_secret": wallet.api_secret.strip(),
        }

    @staticmethod
    def _D(x: Any) -> Decimal:
        if x is None or x == "":
            return Decimal("0")
        return Decimal(str(x))

    async def _server_time_ms(self) -> int:
        now = time.time()
        if self._ts_cached is not None and (now - self._ts_cached_at) < self._ts_ttl_s:
            return self._ts_cached
        r = await self._http.get(f"{self.base_url}/api/v3/time")
        r.raise_for_status()
        server_ms = int(r.json()["serverTime"])
        self._ts_cached = server_ms
        self._ts_cached_at = now
        return server_ms

    async def _signed_get(
        self,
        creds: dict[str, str],
        path: str,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        p = dict(params or {})
        p["timestamp"] = str(await self._server_time_ms())
        p["recvWindow"] = str(self.RECV_WINDOW)
        qs = urlencode(p, doseq=True)
        sig = hex_hmac_sha256(creds["api_secret"], qs)
        url = f"{self.base_url}{path}?{qs}&signature={sig}"
        headers = {"X-MBX-APIKEY": creds["api_key"]}
        r = await self._http.get(url, headers=headers)
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Ourbit error {r.status_code}: {r.text}",
                request=r.request, response=r,
            )
        return r.json()

    async def _get_spot_balances(self, creds: dict[str, str]) -> dict[str, Decimal]:
        data = await self._signed_get(creds, "/api/v3/account")
        out: dict[str, Decimal] = defaultdict(Decimal)
        for b in (data.get("balances") or []):
            asset = (b.get("asset") or "").strip()
            if not asset:
                continue
            total = self._D(b.get("free")) + self._D(b.get("locked"))
            if total != 0:
                out[asset] += total
        return dict(out)

    async def fetch_balance(self, wallet: ExchangeWallet):
        creds = self._creds(wallet)
        spot = await self._get_spot_balances(creds)
        return self._build_result(
            wallet=wallet,
            provider=self.name,
            spot=spot,
            futures={},
            earn={},
        )
