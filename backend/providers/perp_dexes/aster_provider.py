"""Aster DEX — Binance-Futures-compatible REST API (HMAC-SHA256)."""
import hashlib
import hmac
import time
from decimal import Decimal

import httpx

from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider

BASE = "https://fapi.asterdex.com"


def _sign(secret: str, params: dict) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class AsterProvider(BaseWalletProvider):
    name = "AsterProvider"
    label = "Aster"
    enabled = True
    needs_api_key = True
    soon = False   # доступен для добавления

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "Mozilla/5.0"})

    async def aclose(self):
        await self._client.aclose()

    @staticmethod
    def _d(v) -> Decimal:
        try:
            return Decimal(str(v))
        except Exception:
            return Decimal("0")

    async def _signed_get(self, path: str, api_key: str, secret: str, extra: dict | None = None) -> httpx.Response:
        params: dict = {"timestamp": int(time.time() * 1000)}
        if extra:
            params.update(extra)
        params["signature"] = _sign(secret, params)
        return await self._client.get(
            f"{BASE}{path}",
            params=params,
            headers={"X-MBX-APIKEY": api_key},
        )

    async def fetch_balance(self, wallet) -> BalanceResult:
        api_key = wallet.api_key
        secret = wallet.api_secret

        if not api_key or not secret:
            raise ValueError("Aster requires API key and API secret")

        # ── Баланс ───────────────────────────────────────────────────────────
        resp = await self._signed_get("/fapi/v2/balance", api_key, secret)
        resp.raise_for_status()
        data = resp.json()

        totals: dict[str, Decimal] = {}
        for item in (data if isinstance(data, list) else []):
            symbol = (item.get("asset") or "").upper()
            bal = self._d(item.get("balance") or item.get("crossWalletBalance") or 0)
            if symbol and bal > 0:
                totals[symbol] = totals.get(symbol, Decimal("0")) + bal

        # ── Открытые позиции ─────────────────────────────────────────────────
        positions = []
        try:
            pos_resp = await self._signed_get("/fapi/v2/positionRisk", api_key, secret)
            if pos_resp.is_success:
                for pos in (pos_resp.json() if isinstance(pos_resp.json(), list) else []):
                    notional = self._d(pos.get("notionalValue") or pos.get("notional") or 0)
                    if notional == 0:
                        continue
                    positions.append({
                        "symbol": pos.get("symbol", ""),
                        "side": pos.get("positionSide", ""),
                        "notional": str(notional),
                        "unrealized_pnl": str(self._d(pos.get("unRealizedProfit") or pos.get("unrealizedProfit") or 0)),
                        "entry_price": str(self._d(pos.get("entryPrice") or 0)),
                        "mark_price": str(self._d(pos.get("markPrice") or 0)),
                        "leverage": pos.get("leverage", 1),
                    })
        except Exception:
            pass

        totals_str = {k: str(v) for k, v in totals.items() if v > 0}
        return self._build_result(
            wallet, self.name,
            spot=totals_str,
            futures={},
            earn={"positions": positions} if positions else {},
        )
