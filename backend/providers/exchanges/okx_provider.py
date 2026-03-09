import time
from collections import defaultdict
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from backend.domain import ExchangeWallet
from backend.domain.models import BalanceResult
from backend.providers.base import BaseProvider
from settings import settings

from backend.providers.exchanges._signing import b64_hmac_sha256


class OKXProvider(BaseProvider):
    name = "OKXProvider"
    base_url = settings.OKX_BASE_URL  # "https://www.okx.com"

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=20)

        # ✅ cache для server time
        self._ts_cached: str | None = None
        self._ts_cached_at: float = 0.0
        self._ts_ttl_s: float = 25.0

    async def aclose(self) -> None:
        await self._http.aclose()

    def creds_execution(self, wallet: ExchangeWallet) -> dict[str, str]:
        if not wallet.api_key or not wallet.api_secret:
            raise ValueError("OKX api_key/api_secret are required")
        if not wallet.api_passphrase:
            raise ValueError("OKX api_passphrase is required")

        return {
            "api_key": wallet.api_key.strip(),
            "api_secret": wallet.api_secret.strip(),
            "api_passphrase": wallet.api_passphrase.strip(),
        }

    async def _server_ts_iso(self) -> str:
        now = time.time()
        if self._ts_cached and (now - self._ts_cached_at) < self._ts_ttl_s:
            return self._ts_cached

        r = await self._http.get(f"{self.base_url}/api/v5/public/time")
        r.raise_for_status()
        ts_ms = int(r.json()["data"][0]["ts"])

        # ✅ делаем ISO как раньше, но без новых хелперов
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        ts_iso = dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")

        self._ts_cached = ts_iso
        self._ts_cached_at = now
        return ts_iso

    def _okx_headers(self, creds: dict[str, str], method: str, request_path_with_qs: str, ts_iso: str, body: str = "") -> dict[str, str]:
        prehash = f"{ts_iso}{method.upper()}{request_path_with_qs}{body}"
        sign = b64_hmac_sha256(creds["api_secret"], prehash)

        return {
            "OK-ACCESS-KEY": creds["api_key"],
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts_iso,
            "OK-ACCESS-PASSPHRASE": creds["api_passphrase"],
            "Content-Type": "application/json",
        }

    async def _signed_get(self, creds: dict[str, str], path: str, params: Optional[dict[str, Any]] = None) -> dict:
        params = dict(params or {})
        qs = urlencode(params, doseq=True)
        request_path = path + (f"?{qs}" if qs else "")

        ts_iso = await self._server_ts_iso()
        headers = self._okx_headers(creds, "GET", request_path, ts_iso)

        r = await self._http.get(f"{self.base_url}{request_path}", headers=headers)
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(f"OKX error {r.status_code}: {r.text}", request=r.request, response=r)

        data = r.json()
        if data.get("code") != "0":
            raise RuntimeError(f"OKX API error: {data}")
        return data

    @staticmethod
    def _D(x: Any) -> Decimal:
        if x is None or x == "":
            return Decimal("0")
        return Decimal(str(x))

    async def _get_trading_balance(self, creds: dict[str, str]) -> dict[str, Decimal]:
        data = await self._signed_get(creds, "/api/v5/account/balance")
        out = defaultdict(Decimal)
        for acc in data.get("data", []) or []:
            for d in acc.get("details", []) or []:
                ccy = d.get("ccy")
                if not ccy:
                    continue
                cash = self._D(d.get("cashBal"))
                if cash != 0:
                    out[ccy] += cash
        return dict(out)

    async def _get_savings_balance(self, creds: dict[str, str]) -> dict[str, Decimal]:
        data = await self._signed_get(creds, "/api/v5/finance/savings/balance")
        out = defaultdict(Decimal)
        for it in (data.get("data") or []):
            ccy = (it.get("ccy") or "").strip()
            amt = self._D(it.get("amt"))
            if ccy and amt != 0:
                out[ccy] += amt
        return dict(out)

    async def fetch_balance(self, wallet: ExchangeWallet) -> BalanceResult:
        creds = self.creds_execution(wallet)
        try:
            # ✅ параллельно 2 секции
            import asyncio
            trading, savings = await asyncio.gather(
                self._get_trading_balance(creds),
                self._get_savings_balance(creds),
                return_exceptions=True,
            )

            if isinstance(trading, Exception):
                raise trading
            if isinstance(savings, Exception):
                print(f"Error fetching OKX savings: {savings}")
                savings = {}

            totals = defaultdict(Decimal)
            for b in (trading, savings):
                for asset, amt in b.items():
                    totals[asset] += amt

            return BalanceResult(
                wallet=wallet,
                provider=self.name,
                totals={k: str(v) for k, v in totals.items() if v != 0},
                details={
                    "trading": {k: str(v) for k, v in trading.items() if v != 0},
                    "earn": {k: str(v) for k, v in savings.items() if v != 0},
                },
            )
        finally:
            await self.aclose()