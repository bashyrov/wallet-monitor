import asyncio
from collections import defaultdict
import time
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from backend.domain import ExchangeWallet
from backend.domain.models import BalanceResult
from backend.providers.base import BaseProvider
from settings import settings

from backend.providers.exchanges._signing import ms, hex_hmac_sha256


class MexcProvider(BaseProvider):
    name = "MexcProvider"

    # spot base можно держать в settings, futures обычно фиксированный
    spot_base_url = settings.MEXC_BASE_URL  # "https://api.mexc.com"
    futures_base_url = "https://contract.mexc.com"

    SPOT_RECV_WINDOW = 5000
    FUT_RECV_WINDOW_MS = 10_000

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=15)
        self._spot_ts_cached: int | None = None
        self._spot_ts_cached_at: float = 0.0
        self._spot_ts_ttl_s: float = 25.0

    async def aclose(self) -> None:
        await self._http.aclose()

    def _creds(self, wallet: ExchangeWallet) -> dict[str, str]:
        if not wallet.api_key or not wallet.api_secret:
            raise ValueError("MEXC: api_key/api_secret are required")
        return {
            "api_key": wallet.api_key.strip(),
            "api_secret": wallet.api_secret.strip(),
        }

    @staticmethod
    def _D(x: Any) -> Decimal:
        if x is None or x == "":
            return Decimal("0")
        return Decimal(str(x))

    async def _mexc_spot_server_time_ms(self) -> int:
        now = time.time()
        if self._spot_ts_cached is not None and (now - self._spot_ts_cached_at) < self._spot_ts_ttl_s:
            return self._spot_ts_cached

        # MEXC: GET /api/v3/time -> {"serverTime": 171...}
        r = await self._http.get(f"{self.spot_base_url}/api/v3/time")
        r.raise_for_status()
        server_ms = int(r.json()["serverTime"])

        self._spot_ts_cached = server_ms
        self._spot_ts_cached_at = now
        return server_ms

    # ---------- SPOT ----------
    async def _spot_get(self, creds: dict[str, str], path: str, params: Optional[dict[str, Any]] = None) -> dict:
        p = dict(params or {})
        p["timestamp"] = str(await self._mexc_spot_server_time_ms())
        p["recvWindow"] = str(self.SPOT_RECV_WINDOW)

        qs = urlencode(p, doseq=True)
        sig = hex_hmac_sha256(creds["api_secret"], qs)

        url = f"{self.spot_base_url}{path}?{qs}&signature={sig}"
        headers = {"X-MEXC-APIKEY": creds["api_key"]}

        r = await self._http.get(url, headers=headers)

        if r.status_code >= 400:
            # важно видеть тело, там будет msg
            raise httpx.HTTPStatusError(
                f"MEXC SPOT error {r.status_code}: {r.text}",
                request=r.request,
                response=r,
            )

        return r.json()

    async def _get_spot_balances(self, creds: dict[str, str]) -> dict[str, Decimal]:
        data = await self._spot_get(creds, "/api/v3/account", {})
        out = defaultdict(Decimal)

        for b in (data.get("balances") or []):
            asset = (b.get("asset") or "").strip()
            if not asset:
                continue
            free = self._D(b.get("free"))
            locked = self._D(b.get("locked"))
            total = free + locked
            if total != 0:
                out[asset] += total

        return dict(out)

    # ---------- FUTURES ----------
    @staticmethod
    def _futures_param_string(params: dict[str, Any]) -> str:
        if not params:
            return ""
        items = [(k, v) for k, v in params.items() if v is not None]
        items.sort(key=lambda x: x[0])
        return "&".join(f"{k}={v}" for k, v in items)

    async def _futures_get(self, creds: dict[str, str], path: str, params: Optional[dict[str, Any]] = None) -> dict:
        params = dict(params or {})

        ts = str(int(ms()))
        ps = self._futures_param_string(params)

        # signature = HMAC_SHA256(secret, apiKey + timestamp + param_string)
        sign_payload = f"{creds['api_key']}{ts}{ps}"
        sig = hex_hmac_sha256(creds["api_secret"], sign_payload)

        headers = {
            "ApiKey": creds["api_key"],
            "Request-Time": ts,
            "Signature": sig,
            "Recv-Window": str(self.FUT_RECV_WINDOW_MS),
            "Content-Type": "application/json",
        }

        qs = urlencode(params, doseq=True) if params else ""
        url = f"{self.futures_base_url}{path}" + (f"?{qs}" if qs else "")

        r = await self._http.get(url, headers=headers)

        if r.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"MEXC FUTURES error {r.status_code}: {r.text}",
                request=r.request,
                response=r,
            )

        data = r.json()
        # как в примере: бывает {"success": false, ...}
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"MEXC Futures error: {data}")

        return data

    async def _get_futures_equity_by_currency(self, creds: dict[str, str]) -> dict[str, Decimal]:
        data = await self._futures_get(creds, "/api/v1/private/account/assets", {})
        out = defaultdict(Decimal)

        for row in (data.get("data") or []):
            ccy = (row.get("currency") or "").strip()
            if not ccy:
                continue
            equity = self._D(row.get("equity"))
            if equity != 0:
                out[ccy] += equity

        return dict(out)

    # ---------- PUBLIC ----------
    async def fetch_balance(self, wallet: ExchangeWallet) -> BalanceResult:
        creds = self._creds(wallet)

        try:
            # ✅ быстрее: spot + futures параллельно
            spot_task = self._get_spot_balances(creds)
            fut_task = self._get_futures_equity_by_currency(creds)

            spot, futures = await asyncio.gather(spot_task, fut_task, return_exceptions=True)

            if isinstance(spot, Exception):
                raise spot
            if isinstance(futures, Exception):
                # futures не всегда включены/разрешены, не валим весь результат
                print(f"Error fetching MEXC futures: {futures}")
                futures = {}

            totals = defaultdict(Decimal)
            for b in (spot, futures):
                for asset, amt in b.items():
                    totals[asset] += amt

            return BalanceResult(
                wallet=wallet,
                provider=self.name,
                totals={k: str(v) for k, v in totals.items() if v != 0},
                details={
                    "spot": {k: str(v) for k, v in spot.items() if v != 0},
                    "futures": {k: str(v) for k, v in futures.items() if v != 0},
                },
            )

        finally:
            await self.aclose()