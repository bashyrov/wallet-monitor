"""
WhiteBIT exchange provider.
Auth: X-TXC-APIKEY / X-TXC-SIGNATURE / X-TXC-NONCE
Signature = HMAC-SHA512(hex) over base64-encoded JSON body.
Main account (spot/funding): POST /api/v4/main-account/balance
Trade account (active orders margin): POST /api/v4/trade-account/balance
Collateral (futures): POST /api/v4/collateral-account/balance
"""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections import defaultdict
from decimal import Decimal

from backend.domain import ExchangeWallet
from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider
from backend.providers.http import RetryClient
from settings import settings


class WhiteBITProvider(BaseWalletProvider):
    name = "WhiteBITProvider"
    label = "WhiteBIT"
    enabled = True
    needs_passphrase = False
    base_url = settings.WHITEBIT_BASE_URL  # "https://whitebit.com"

    def __init__(self) -> None:
        self._http = RetryClient(timeout=12)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _signed_request(self, wallet: ExchangeWallet, path: str, extra: dict | None = None):
        nonce = str(int(time.time() * 1000))
        body_dict = {"request": path, "nonce": nonce, **(extra or {})}
        body_json = json.dumps(body_dict, separators=(",", ":"))
        b64_body = base64.b64encode(body_json.encode()).decode()
        sign = hmac.new(
            wallet.api_secret.strip().encode(),
            b64_body.encode(),
            hashlib.sha512,
        ).hexdigest()
        headers = {
            "X-TXC-APIKEY": wallet.api_key.strip(),
            "X-TXC-PAYLOAD": b64_body,
            "X-TXC-SIGNATURE": sign,
            "X-TXC-NONCE": nonce,
            "Content-Type": "application/json",
        }
        return self._http.post(
            f"{self.base_url}{path}",
            content=body_json.encode(),
            headers=headers,
        )

    # ── Balance endpoints ─────────────────────────────────────────────────────

    async def _get_main(self, wallet: ExchangeWallet) -> dict[str, Decimal]:
        """Main account — spot/funding balances."""
        r = await self._signed_request(wallet, "/api/v4/main-account/balance")
        r.raise_for_status()
        data = r.json()
        # response: {"CURRENCY": {"main_balance": "1.0"}, ...}
        out: dict[str, Decimal] = {}
        for asset, info in (data if isinstance(data, dict) else {}).items():
            if asset in ("success", "message"):
                continue
            val = Decimal(str(info.get("main_balance") or "0"))
            if val > 0:
                out[asset.upper()] = val
        return out

    async def _get_trade(self, wallet: ExchangeWallet) -> dict[str, Decimal]:
        """Trade account — spot balance available for trading."""
        r = await self._signed_request(wallet, "/api/v4/trade-account/balance")
        r.raise_for_status()
        data = r.json()
        out: dict[str, Decimal] = {}
        for asset, info in (data if isinstance(data, dict) else {}).items():
            if asset in ("success", "message"):
                continue
            available = Decimal(str(info.get("available") or "0"))
            freeze = Decimal(str(info.get("freeze") or "0"))
            val = available + freeze
            if val > 0:
                out[asset.upper()] = val
        return out

    async def _get_collateral(self, wallet: ExchangeWallet) -> tuple[dict[str, Decimal], Decimal]:
        """Collateral (futures) account balance + unrealised PnL."""
        try:
            r = await self._signed_request(wallet, "/api/v4/collateral-account/balance")
            r.raise_for_status()
            data = r.json()
            out: dict[str, Decimal] = {}
            upnl = Decimal("0")
            for asset, info in (data if isinstance(data, dict) else {}).items():
                if asset in ("success", "message"):
                    continue
                available = Decimal(str(info.get("available") or "0"))
                freeze = Decimal(str(info.get("freeze") or "0"))
                val = available + freeze
                if val > 0:
                    out[asset.upper()] = val
            # Try to get unrealised PnL from open positions summary
            try:
                r2 = await self._signed_request(wallet, "/api/v4/collateral-account/positions/summary")
                r2.raise_for_status()
                summary = r2.json()
                if isinstance(summary, dict):
                    upnl = Decimal(str(summary.get("unrealizedFunding") or summary.get("unrealizedPnl") or "0"))
                elif isinstance(summary, list):
                    for pos in summary:
                        upnl += Decimal(str(pos.get("unrealizedFunding") or pos.get("pnl") or "0"))
            except Exception:
                pass
            return out, upnl
        except Exception:
            return {}, Decimal("0")

    # ── Main ─────────────────────────────────────────────────────────────────

    async def fetch_balance(self, wallet: ExchangeWallet) -> BalanceResult:
        main_res, trade_res, collateral_res = await asyncio.gather(
            self._get_main(wallet),
            self._get_trade(wallet),
            self._get_collateral(wallet),
            return_exceptions=True,
        )
        if isinstance(main_res, Exception) and isinstance(trade_res, Exception):
            raise main_res

        spot: defaultdict = defaultdict(Decimal)
        for src in (main_res, trade_res):
            if isinstance(src, dict):
                for k, v in src.items():
                    spot[k] += v

        futures: dict[str, Decimal] = {}
        upnl: Decimal = Decimal("0")
        if not isinstance(collateral_res, Exception):
            futures, upnl = collateral_res

        upnl_str = str(upnl) if upnl != 0 else None
        return self._build_result(wallet, self.name, dict(spot), futures, {}, upnl_usd=upnl_str)
