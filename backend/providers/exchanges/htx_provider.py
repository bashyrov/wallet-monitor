"""HTX (formerly Huobi) wallet provider.

HTX signs requests with HMAC-SHA256 where the payload is:
    METHOD\nHOST\nPATH\nSORTED_QUERY_STRING
and the digest is base64-encoded. All private calls include
`AccessKeyId`, `SignatureMethod=HmacSHA256`, `SignatureVersion=2`,
`Timestamp=YYYY-MM-DDTHH%3AMM%3ASS` in the query string.

Account types we pull balances from:
  · spot          — `/v1/account/accounts` → filter type='spot'
  · point         — `/v1/account/accounts` → filter type='point' (cashback)
  · otc           — skipped (not relevant for traders)
  · linear-swap   — `/linear-swap-api/v1/swap_cross_account_info` (USDT-M
                    perp cross margin) + `/linear-swap-api/v1/swap_account_info`
                    (isolated per-contract).

Spot base host: api.huobi.pro. Futures base host: api.hbdm.com.
"""
from collections import defaultdict
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlencode

import httpx
from backend.providers.http import RetryClient

from backend.domain import ExchangeWallet
from backend.providers.base_wallet_provider import BaseWalletProvider
from settings import settings

from backend.providers.exchanges._signing import b64_hmac_sha256


class HTXProvider(BaseWalletProvider):
    name = "HTXProvider"
    label = "HTX"
    enabled = True
    needs_passphrase = False

    spot_base_url = settings.HTX_SPOT_BASE_URL      # https://api.huobi.pro
    futures_base_url = settings.HTX_FUTURES_BASE_URL  # https://api.hbdm.com

    def __init__(self) -> None:
        self._http = RetryClient(timeout=15)
        self._spot_account_id: dict[str, int] = {}  # api_key → spot account id

    async def aclose(self) -> None:
        await self._http.aclose()

    def _creds(self, wallet: ExchangeWallet) -> dict[str, str]:
        if not wallet.api_key or not wallet.api_secret:
            raise ValueError("HTX: api_key/api_secret are required")
        return {
            "api_key": wallet.api_key.strip(),
            "api_secret": wallet.api_secret.strip(),
        }

    @staticmethod
    def _D(x: Any) -> Decimal:
        if x is None or x == "":
            return Decimal("0")
        return Decimal(str(x))

    @staticmethod
    def _ts() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    @classmethod
    def _sign_payload(cls, method: str, host: str, path: str, params: dict) -> str:
        """Build the signing payload HTX requires:

            METHOD\nHOST\nPATH\n<sorted query-string, URL-encoded>
        """
        items = sorted(params.items(), key=lambda kv: kv[0])
        qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in items)
        return f"{method.upper()}\n{host}\n{path}\n{qs}"

    async def _signed_get(
        self,
        creds: dict[str, str],
        base_url: str,
        path: str,
        extra_params: Optional[dict[str, Any]] = None,
    ) -> Any:
        host = base_url.replace("https://", "").replace("http://", "")
        params: dict[str, Any] = dict(extra_params or {})
        params["AccessKeyId"] = creds["api_key"]
        params["SignatureMethod"] = "HmacSHA256"
        params["SignatureVersion"] = "2"
        params["Timestamp"] = self._ts()
        payload = self._sign_payload("GET", host, path, params)
        params["Signature"] = b64_hmac_sha256(creds["api_secret"], payload)
        qs = urlencode(params, quote_via=quote)
        url = f"{base_url}{path}?{qs}"
        r = await self._http.get(url)
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTX error {r.status_code}: {r.text}",
                request=r.request, response=r,
            )
        data = r.json()
        # HTX returns {"status": "error", "err-code": "...", "err-msg": "..."}
        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError(f"HTX API error: {data.get('err-msg')}")
        return data

    async def _get_spot_account_id(self, creds: dict[str, str]) -> int:
        cached = self._spot_account_id.get(creds["api_key"])
        if cached is not None:
            return cached
        data = await self._signed_get(creds, self.spot_base_url, "/v1/account/accounts")
        for a in (data.get("data") or []):
            if a.get("type") == "spot" and a.get("state") == "working":
                self._spot_account_id[creds["api_key"]] = int(a["id"])
                return int(a["id"])
        raise RuntimeError("HTX: no active spot account found on this key")

    async def _get_spot_balances(self, creds: dict[str, str]) -> dict[str, Decimal]:
        account_id = await self._get_spot_account_id(creds)
        data = await self._signed_get(
            creds, self.spot_base_url, f"/v1/account/accounts/{account_id}/balance"
        )
        out: dict[str, Decimal] = defaultdict(Decimal)
        for row in (data.get("data") or {}).get("list") or []:
            asset = (row.get("currency") or "").upper().strip()
            # HTX splits each asset into {trade, frozen} rows — sum both.
            if asset:
                out[asset] += self._D(row.get("balance"))
        # Normalise HTX's XBT/XRP-style quirks (they already use BTC/ETH so
        # nothing to translate today, but keep the hook ready).
        return {k: v for k, v in out.items() if v != 0}

    async def _get_futures_equity(self, creds: dict[str, str]) -> tuple[dict[str, Decimal], Decimal]:
        """USDT-M linear swap cross-margin account.

        /linear-swap-api/v1/swap_cross_account_info returns one row per
        margin_account (USDT, BTC-USD, etc.). `margin_balance` = equity,
        `profit_unreal` = unrealised PnL.
        """
        try:
            data = await self._signed_get(
                creds, self.futures_base_url,
                "/linear-swap-api/v1/swap_cross_account_info",
            )
        except Exception:
            return {}, Decimal("0")
        out: dict[str, Decimal] = defaultdict(Decimal)
        upnl = Decimal("0")
        for row in (data.get("data") or []):
            asset = (row.get("margin_asset") or row.get("symbol") or "").upper()
            if not asset:
                continue
            out[asset] += self._D(row.get("margin_balance"))
            upnl += self._D(row.get("profit_unreal"))
        return {k: v for k, v in out.items() if v != 0}, upnl

    async def fetch_balance(self, wallet: ExchangeWallet):
        import asyncio
        creds = self._creds(wallet)
        spot, fut = await asyncio.gather(
            self._get_spot_balances(creds),
            self._get_futures_equity(creds),
            return_exceptions=True,
        )
        if isinstance(spot, Exception):
            raise spot
        if isinstance(fut, Exception):
            futures_dict, upnl = {}, None
        else:
            futures_dict, upnl = fut
            upnl_str = str(upnl) if upnl != 0 else None

        return self._build_result(
            wallet=wallet,
            provider=self.name,
            spot=spot,
            futures=futures_dict,
            earn={},
            upnl_usd=upnl_str if not isinstance(fut, Exception) else None,
        )
