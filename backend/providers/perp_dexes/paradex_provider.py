"""Paradex balance provider — Portfolio page.

Two paths to read balance:

  1. SNIP-12 sign via go-fetcher (preferred). When the wallet has an
     L2 private key stored we delegate to /internal/trade/balance which
     produces a fresh 4-min JWT and queries /v1/balance. This is the
     same code path the Screener uses for trading and survives the
     5-min server-side JWT cap.

  2. Legacy `api_token` (JWT pasted by the user from paradex.trade).
     Kept for users who set up Paradex before the SNIP-12 work landed
     and never added a private key. JWT lifetime is 5 min so the
     refresh window is brief — surface a clear "JWT expired" error
     when it happens.
"""
import asyncio
from collections import defaultdict
from decimal import Decimal

import httpx
from backend.providers.http import RetryClient

from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider


class ParadexProvider(BaseWalletProvider):
    name = "ParadexProvider"
    label = "Paradex"
    enabled = True
    needs_api_key = False
    # api_token is now optional (we mint JWTs ourselves from the L2
    # private key). UI hint stays so users can still paste one.
    needs_api_token = False
    needs_l2_private_key = True
    base_url = "https://api.prod.paradex.trade"

    def __init__(self):
        self._client = RetryClient(timeout=20.0)

    async def aclose(self):
        await self._client.aclose()

    @staticmethod
    def _d(value):
        if value in (None, "", False):
            return Decimal("0")
        return Decimal(str(value))

    async def fetch_balance(self, wallet) -> BalanceResult:
        # Prefer the Go SNIP-12 path when the L2 private key is stored.
        priv = getattr(wallet, "private_key", None)
        if priv:
            try:
                from backend.services import trade_proxy
                creds = {
                    "address":        getattr(wallet, "address", "") or "",
                    "api_key":        getattr(wallet, "address", "") or "",  # paradex maps address → api_key for Go
                    "private_key":    priv,
                    "api_secret":     priv,                                  # canonical Go field
                    "api_passphrase": getattr(wallet, "api_passphrase", None) or "",
                }
                bal = await trade_proxy.fetch_balance("paradex", creds)
                # Go returns USD totals — surface as USDC for the per-asset table.
                usdc = float(bal.get("total") or bal.get("usdt") or 0)
                totals = {"USDC": str(usdc)} if usdc > 0 else {}
                return BalanceResult(wallet=wallet, provider=self.name,
                                     totals=totals, details={"assets": totals, "source": "snip12"})
            except trade_proxy.GoTradeError as e:
                msg = str(e)
                if "401" in msg or "403" in msg or "Unauthorized" in msg.lower() or "forbidden" in msg.lower():
                    raise ValueError(f"Paradex rejected the L2 key: {msg[:200]}")
                # Network blip — surface so the dashboard shows the error chip
                raise
            finally:
                await self.aclose()

        # Legacy JWT-only path — kept for users without a private key.
        jwt_token = getattr(wallet, "api_token", None) or getattr(wallet, "jwt_token", None)
        if not jwt_token:
            raise ValueError("Paradex wallet needs either an L2 private key (recommended — also enables Screener) or an api_token from paradex.trade.")
        try:
            url = f"{self.base_url}/v1/balance"
            headers = {"accept": "application/json", "authorization": f"Bearer {jwt_token}"}
            resp = await self._client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") or []

            totals = defaultdict(Decimal)
            for item in results:
                token = (item.get("token") or "").strip().upper()
                size = self._d(item.get("size"))
                if token and size > 0:
                    totals[token] += size

            filtered_assets = {k: str(v) for k, v in totals.items() if v > 0}
            return BalanceResult(wallet=wallet, provider=self.name,
                                 totals=filtered_assets,
                                 details={"assets": filtered_assets, "source": "jwt"})
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 401:
                raise ValueError("Paradex JWT expired (5-min cap). Re-paste from paradex.trade or add an L2 private key for self-renewing auth.")
            raise
        finally:
            await self.aclose()
