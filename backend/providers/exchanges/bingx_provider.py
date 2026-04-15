"""
BingX exchange provider.
Auth: HMAC-SHA256, timestamp + signature appended to query string.
Spot: GET /openApi/spot/v1/account/balance
Perp futures: GET /openApi/swap/v2/user/balance
Standard futures: GET /openApi/contract/v1/balance
"""
import asyncio
import hashlib
import hmac
import time
from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlencode

from backend.domain import ExchangeWallet
from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider
from backend.providers.http import RetryClient
from settings import settings


class BingXProvider(BaseWalletProvider):
    name = "BingXProvider"
    label = "BingX"
    enabled = True
    needs_passphrase = False
    base_url = settings.BINGX_BASE_URL  # "https://open-api.bingx.com"

    def __init__(self) -> None:
        self._http = RetryClient(timeout=12)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _signed_url(self, wallet: ExchangeWallet, path: str, params: dict | None = None) -> tuple[str, dict]:
        ts = str(int(time.time() * 1000))
        p = dict(params or {})
        # Build sorted param string then append timestamp (BingX requirement)
        qs = urlencode(sorted(p.items())) if p else ""
        payload = (qs + "&" if qs else "") + f"timestamp={ts}"
        sig = hmac.new(
            wallet.api_secret.strip().encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        url = f"{self.base_url}{path}?{payload}&signature={sig}"
        headers = {"X-BX-APIKEY": wallet.api_key.strip()}
        return url, headers

    # ── Balance endpoints ─────────────────────────────────────────────────────

    async def _get_spot(self, wallet: ExchangeWallet) -> dict[str, Decimal]:
        url, headers = self._signed_url(wallet, "/openApi/spot/v1/account/balance")
        r = await self._http.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
        out: dict[str, Decimal] = {}
        for b in (data.get("data") or {}).get("balances") or []:
            asset = (b.get("asset") or "").upper()
            free = Decimal(str(b.get("free") or "0"))
            locked = Decimal(str(b.get("locked") or "0"))
            val = free + locked
            if asset and val > 0:
                out[asset] = out.get(asset, Decimal(0)) + val
        return out

    async def _get_perp(self, wallet: ExchangeWallet) -> tuple[dict[str, Decimal], Decimal]:
        """Perpetual futures (SWAP) balance."""
        try:
            url, headers = self._signed_url(wallet, "/openApi/swap/v2/user/balance")
            r = await self._http.get(url, headers=headers)
            r.raise_for_status()
            bal = (r.json().get("data") or {}).get("balance") or {}
            asset = (bal.get("asset") or "USDT").upper()
            total = Decimal(str(bal.get("balance") or "0"))
            upnl = Decimal(str(bal.get("unrealizedProfit") or "0"))
            out = {asset: total} if total > 0 else {}
            return out, upnl
        except Exception:
            return {}, Decimal("0")

    async def _get_standard_futures(self, wallet: ExchangeWallet) -> dict[str, Decimal]:
        """Standard (coin-margined) futures balance."""
        try:
            url, headers = self._signed_url(wallet, "/openApi/contract/v1/balance")
            r = await self._http.get(url, headers=headers)
            r.raise_for_status()
            out: dict[str, Decimal] = {}
            for item in r.json().get("data") or []:
                asset = (item.get("asset") or "").upper()
                available = Decimal(str(item.get("availableMargin") or "0"))
                locked = Decimal(str(item.get("usedMargin") or "0"))
                val = available + locked
                if asset and val > 0:
                    out[asset] = out.get(asset, Decimal(0)) + val
            return out
        except Exception:
            return {}

    # ── Main ─────────────────────────────────────────────────────────────────

    async def fetch_balance(self, wallet: ExchangeWallet) -> BalanceResult:
        spot_res, perp_res, std_res = await asyncio.gather(
            self._get_spot(wallet),
            self._get_perp(wallet),
            self._get_standard_futures(wallet),
            return_exceptions=True,
        )
        if isinstance(spot_res, Exception):
            raise spot_res

        futures: defaultdict = defaultdict(Decimal)
        upnl = Decimal("0")

        if not isinstance(perp_res, Exception):
            perp_bal, perp_upnl = perp_res
            for k, v in perp_bal.items():
                futures[k] += v
            upnl += perp_upnl

        if not isinstance(std_res, Exception):
            for k, v in std_res.items():
                futures[k] += v

        upnl_str = str(upnl) if upnl != 0 else None
        return self._build_result(wallet, self.name, spot_res, dict(futures), {}, upnl_usd=upnl_str)
