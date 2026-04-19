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

    async def _get_futures(self, wallet: ExchangeWallet, settle: str) -> tuple[dict[str, Decimal], Decimal]:
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
        upnl = Decimal(str(data.get("unrealised_pnl") or "0"))
        return ({currency: total} if total > 0 else {}), upnl

    async def _get_earn(self, wallet: ExchangeWallet) -> dict[str, Decimal]:
        """Gate.io Uni Lending positions (Simple Earn has no public API)"""
        path = "/api/v4/earn/uni/lends"
        try:
            r = await self._http.get(
                f"{self.base_url}{path}",
                headers=self._headers(wallet, "GET", path),
            )
            r.raise_for_status()
            raw = r.json()
            out = defaultdict(Decimal)
            for item in raw if isinstance(raw, list) else []:
                currency = (item.get("currency") or "").upper()
                amount = Decimal(str(item.get("amount") or "0"))
                if currency and amount > 0:
                    out[currency] += amount
            return dict(out)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Gate.io earn fetch failed: %s", e)
            return {}

    async def _get_unified(self, wallet: ExchangeWallet) -> dict[str, Decimal]:
        """Gate.io Unified Trading Account (единый торговый счёт). Returns
        a single object with a `balances` dict keyed by currency. Most
        active traders now keep their funds here rather than in legacy
        Spot wallets."""
        path = "/api/v4/unified/accounts"
        try:
            r = await self._http.get(
                f"{self.base_url}{path}",
                headers=self._headers(wallet, "GET", path),
            )
            r.raise_for_status()
            data = r.json() or {}
            out = defaultdict(Decimal)
            # Response shape: {"balances": {"USDT": {"available": "...",
            # "freeze": "...", "borrowed": "...", ...}, ...}, ...}
            balances = data.get("balances") or {}
            for ccy, info in balances.items():
                if not isinstance(info, dict):
                    continue
                available = Decimal(str(info.get("available") or "0"))
                freeze    = Decimal(str(info.get("freeze")    or "0"))
                total = available + freeze
                if total > 0:
                    out[ccy.upper()] += total
            return dict(out)
        except Exception as e:
            import logging
            # 404 is expected for accounts that haven't opted into unified
            logging.getLogger(__name__).warning("Gate.io unified fetch failed: %s", e)
            return {}

    async def _get_margin(self, wallet: ExchangeWallet) -> dict[str, Decimal]:
        """Gate.io cross-margin account. Optional — many users keep funds
        here for spot-margin trading."""
        path = "/api/v4/margin/cross/accounts"
        try:
            r = await self._http.get(
                f"{self.base_url}{path}",
                headers=self._headers(wallet, "GET", path),
            )
            r.raise_for_status()
            data = r.json() or {}
            out = defaultdict(Decimal)
            balances = data.get("balances") or {}
            for ccy, info in balances.items():
                if not isinstance(info, dict):
                    continue
                available = Decimal(str(info.get("available") or "0"))
                freeze    = Decimal(str(info.get("freeze")    or "0"))
                total = available + freeze
                if total > 0:
                    out[ccy.upper()] += total
            return dict(out)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Gate.io cross-margin fetch failed: %s", e)
            return {}

    async def fetch_balance(self, wallet: ExchangeWallet):
        spot, fut_usdt, fut_btc, earn, unified, margin = await asyncio.gather(
            self._get_spot(wallet),
            self._get_futures(wallet, "usdt"),
            self._get_futures(wallet, "btc"),
            self._get_earn(wallet),
            self._get_unified(wallet),
            self._get_margin(wallet),
            return_exceptions=True,
        )

        if isinstance(spot, Exception):
            raise spot

        # Gate's Unified Trading Account is a MODE, not an extra wallet.
        # When a user is in Unified, the legacy /spot/accounts endpoint
        # still reports the same funds — summing both double-counts. So:
        #   · if Unified is non-empty, trust it as the single spot-side
        #     source (it already rolls up spot + margin + cross).
        #   · otherwise, fall back to spot (+ cross-margin if populated).
        unified_dict = unified if not isinstance(unified, Exception) else {}
        margin_dict  = margin  if not isinstance(margin,  Exception) else {}

        spot_merged: dict[str, Decimal] = defaultdict(Decimal)
        if unified_dict:
            for k, v in unified_dict.items():
                spot_merged[k] += v
        else:
            for k, v in (spot or {}).items():
                spot_merged[k] += v
            for k, v in margin_dict.items():
                spot_merged[k] += v

        futures: dict[str, Decimal] = defaultdict(Decimal)
        upnl = Decimal("0")
        for f in (fut_usdt, fut_btc):
            if not isinstance(f, Exception):
                bal, pnl = f
                for k, v in bal.items():
                    futures[k] += v
                upnl += pnl

        earn_dict = earn if not isinstance(earn, Exception) else {}
        upnl_str = str(upnl) if upnl != 0 else None
        return self._build_result(wallet, self.name, dict(spot_merged), dict(futures), earn_dict, upnl_usd=upnl_str)