import asyncio
from collections import defaultdict
import time
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from backend.providers.http import RetryClient

from backend.domain import ExchangeWallet
from backend.providers.base_wallet_provider import BaseWalletProvider
from settings import settings

from backend.providers.exchanges._signing import ms, hex_hmac_sha256


class MexcProvider(BaseWalletProvider):
    name = "MexcProvider"
    label = "MEXC"
    enabled = True
    needs_passphrase = False

    spot_base_url = settings.MEXC_BASE_URL  # "https://api.mexc.com"
    futures_base_url = "https://contract.mexc.com"

    SPOT_RECV_WINDOW = 5000
    FUT_RECV_WINDOW_MS = 10_000

    def __init__(self) -> None:
        self._http = RetryClient(timeout=15)
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

        r = await self._http.get(f"{self.spot_base_url}/api/v3/time")
        r.raise_for_status()
        server_ms = int(r.json()["serverTime"])

        self._spot_ts_cached = server_ms
        self._spot_ts_cached_at = now
        return server_ms

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

    async def spot_avg_entry(
        self,
        creds: dict[str, str],
        symbol: str,
        target_qty: float,
    ) -> Optional[float]:
        """Compute the cost basis for the user's current spot holding of
        `symbol` against USDT.

        Walks /api/v3/myTrades from newest to oldest, accumulating BUY
        fills (and netting against SELL fills along the way) until we've
        covered `target_qty`. Returns the weighted-average buy price.

        Returns None on auth/permission failure or if buys don't cover
        the target qty (caller falls back to short.entry).
        """
        if target_qty <= 0:
            return None
        sym = symbol.upper().rstrip("USDT") + "USDT"
        try:
            data = await self._spot_get(creds, "/api/v3/myTrades", {
                "symbol": sym,
                "limit": "500",
            })
        except Exception:
            return None
        trades = data if isinstance(data, list) else (data or {}).get("trades") or []
        if not trades:
            return None
        # MEXC returns oldest→newest; reverse for FIFO-from-newest accumulation.
        # Newest BUYs are most likely to match the current holding qty.
        trades = sorted(trades, key=lambda t: int(t.get("time") or 0), reverse=True)
        remaining = float(target_qty)
        cost = 0.0
        filled = 0.0
        # Track running net qty so SELL trades reduce buy obligation.
        for t in trades:
            try:
                q = float(t.get("qty") or 0)
                p = float(t.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if q <= 0 or p <= 0:
                continue
            is_buyer = bool(t.get("isBuyer"))
            if is_buyer:
                take = min(q, remaining)
                cost += take * p
                filled += take
                remaining -= take
                if remaining <= 1e-9:
                    break
            else:
                # SELL — releases earlier buy obligation. We're walking
                # newest→oldest so a recent sell offsets that-much qty
                # of the older buys we'd otherwise count.
                remaining += q
        if filled <= 0:
            return None
        return cost / filled

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
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"MEXC Futures error: {data}")

        return data

    async def _get_futures_equity_by_currency(self, creds: dict[str, str]) -> tuple[dict[str, Decimal], Decimal]:
        data = await self._futures_get(creds, "/api/v1/private/account/assets", {})
        out = defaultdict(Decimal)
        upnl = Decimal("0")

        for row in (data.get("data") or []):
            ccy = (row.get("currency") or "").strip()
            if not ccy:
                continue
            equity = self._D(row.get("equity"))
            if equity != 0:
                out[ccy] += equity
            upnl += self._D(row.get("unrealized"))

        return dict(out), upnl

    async def fetch_balance(self, wallet: ExchangeWallet):
        creds = self._creds(wallet)

        spot, futures = await asyncio.gather(
            self._get_spot_balances(creds),
            self._get_futures_equity_by_currency(creds),
            return_exceptions=True,
        )

        # Both legs are tolerated — MEXC frequently has asymmetric
        # permissions (IP whitelist applied per key, futures-only keys,
        # spot-disabled accounts). Failing the whole balance because one
        # leg 406'd hides a working leg from the user. We log the
        # specific error so support can see what to fix (typically
        # whitelist the prod IP), but the result still has the partial
        # data.
        spot_dict: dict[str, Decimal] = {}
        futures_dict: dict[str, Decimal] = {}
        upnl = None
        spot_err = futures_err = None
        if isinstance(spot, Exception):
            spot_err = spot
        else:
            spot_dict = spot
        if isinstance(futures, Exception):
            futures_err = futures
        else:
            futures_dict, raw_upnl = futures
            upnl = str(raw_upnl) if raw_upnl != 0 else None

        # If both failed, surface ONE of them so the caller registers a
        # provider error (otherwise the wallet looks empty silently).
        if spot_err and futures_err:
            raise spot_err

        # Partial-success case — log the failed leg with enough context
        # for the user to act on it (usually IP whitelist).
        if spot_err:
            import logging
            logging.getLogger("avalant.providers.mexc").warning(
                "MEXC spot balance failed (continuing with futures only) wallet=%s: %s",
                wallet.id, spot_err,
            )
        if futures_err:
            import logging
            logging.getLogger("avalant.providers.mexc").warning(
                "MEXC futures balance failed (continuing with spot only) wallet=%s: %s",
                wallet.id, futures_err,
            )

        return self._build_result(wallet, self.name, spot_dict, futures_dict, {}, upnl_usd=upnl)