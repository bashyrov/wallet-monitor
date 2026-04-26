import asyncio
import logging
import time

from backend.providers.http import RetryClient
from collections import defaultdict
from decimal import Decimal

from backend.domain import ExchangeWallet
from backend.providers.base_wallet_provider import BaseWalletProvider
from settings import settings

from ._signing import b64_hmac_sha256

logger = logging.getLogger("avalant.providers.kucoin")


class KucoinProvider(BaseWalletProvider):
    name = "KucoinProvider"
    label = "KuCoin"
    enabled = True
    needs_passphrase = True
    base_url = settings.KUCOIN_BASE_URL  # "https://api.kucoin.com"
    futures_base_url = "https://api-futures.kucoin.com"
    key_version = "2"

    def __init__(self) -> None:
        self._http = RetryClient(timeout=10)
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

    async def _get_spot(self, wallet: ExchangeWallet) -> dict[str, Decimal]:
        """Spot + margin + main accounts"""
        path = "/api/v1/accounts"
        r = await self._http.get(
            f"{self.base_url}{path}",
            headers=await self._headers(wallet, "GET", path),
        )
        r.raise_for_status()
        out = defaultdict(Decimal)
        for x in r.json().get("data", []):
            amt = Decimal(str(x["balance"]))
            if amt > 0:
                out[x["currency"]] += amt
        return dict(out)

    async def _get_futures(self, wallet: ExchangeWallet) -> tuple[dict[str, Decimal], str | None]:
        """KuCoin Futures account (separate domain).

        KuCoin maintains a SEPARATE account-overview balance per margin
        currency — USDT-margined, USDC-margined, XBT-margined are each their
        own pot. The API's GET /api/v1/account-overview requires a
        `?currency=` query, and without it the response defaults to USDT
        only — which is why USDC / BTC futures balances were silently
        missing from the portfolio fetch.

        Fetch all three margins in parallel, merge equity per currency,
        sum unrealizedPnl across them. Skip empty pots so we don't pad
        the wallet view with zero-equity rows."""
        currencies = ("USDT", "USDC", "XBT")

        async def _one(cur: str) -> tuple[str, Decimal, Decimal | None]:
            path = f"/api/v1/account-overview?currency={cur}"
            try:
                r = await self._http.get(
                    f"{self.futures_base_url}{path}",
                    headers=await self._headers(wallet, "GET", path),
                )
                r.raise_for_status()
            except Exception as exc:
                logger.info("kucoin futures fetch %s failed: %s", cur, exc)
                return cur, Decimal("0"), None
            data = r.json().get("data") or {}
            equity = Decimal(str(data.get("accountEquity") or "0"))
            upnl = data.get("unrealisedPnl")
            upnl_d: Decimal | None
            try:
                upnl_d = Decimal(str(upnl)) if upnl is not None else None
            except Exception:
                upnl_d = None
            return cur, equity, upnl_d

        results = await asyncio.gather(*(_one(c) for c in currencies), return_exceptions=True)
        out: dict[str, Decimal] = {}
        upnl_total: Decimal | None = None
        for r in results:
            if isinstance(r, Exception):
                continue
            cur, equity, upnl_d = r
            asset = "BTC" if cur == "XBT" else cur
            if equity > 0:
                out[asset] = out.get(asset, Decimal("0")) + equity
            if upnl_d is not None:
                upnl_total = (upnl_total or Decimal("0")) + upnl_d
        upnl_str = str(upnl_total) if upnl_total is not None else None
        return out, upnl_str

    async def fetch_balance(self, wallet: ExchangeWallet):
        spot, futures = await asyncio.gather(
            self._get_spot(wallet),
            self._get_futures(wallet),
            return_exceptions=True,
        )

        if isinstance(spot, Exception):
            raise spot

        if isinstance(futures, Exception):
            futures_dict, upnl_str = {}, None
        else:
            futures_dict, upnl_str = futures

        return self._build_result(wallet, self.name, dict(spot), futures_dict, {}, upnl_usd=upnl_str)