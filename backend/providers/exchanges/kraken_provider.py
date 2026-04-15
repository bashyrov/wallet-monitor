"""
Kraken exchange provider.
Auth: nonce-based HMAC-SHA512 (POST, application/x-www-form-urlencoded).
Spot: POST /0/private/Balance
Earn: POST /0/private/Earn/Allocations (paginated)
Futures: skipped — separate domain/auth (futures.kraken.com)
"""
import asyncio
import base64
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

# Kraken wraps asset names with X/Z prefixes for legacy reasons
_ASSET_RENAME = {
    "XXBT": "BTC", "XBT": "BTC",
    "XETH": "ETH",  "XXLM": "XLM", "XLTC": "LTC",
    "XXMR": "XMR",  "XXRP": "XRP", "XZEC": "ZEC",
    "ZUSD": "USD",  "ZEUR": "EUR",  "ZGBP": "GBP",
    "ZCAD": "CAD",  "ZJPY": "JPY",
}


def _norm(asset: str) -> str:
    """Normalise Kraken internal asset name → common ticker."""
    return _ASSET_RENAME.get(asset, asset)


class KrakenProvider(BaseWalletProvider):
    name = "KrakenProvider"
    label = "Kraken"
    enabled = True
    needs_passphrase = False
    base_url = settings.KRAKEN_BASE_URL  # "https://api.kraken.com"

    def __init__(self) -> None:
        self._http = RetryClient(timeout=12)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _sign(self, url_path: str, post_data: str, nonce: str) -> str:
        """Kraken HMAC-SHA512 signature."""
        sha256_hash = hashlib.sha256((nonce + post_data).encode()).digest()
        mac = hmac.new(
            base64.b64decode(self._secret),
            url_path.encode() + sha256_hash,
            hashlib.sha512,
        )
        return base64.b64encode(mac.digest()).decode()

    def _post(self, wallet: ExchangeWallet, path: str, extra: dict | None = None):
        self._secret = wallet.api_secret.strip()
        nonce = str(int(time.time() * 1000))
        params = {"nonce": nonce, **(extra or {})}
        body = urlencode(params)
        sign = self._sign(path, body, nonce)
        headers = {
            "API-Key": wallet.api_key.strip(),
            "API-Sign": sign,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        return self._http.post(f"{self.base_url}{path}", content=body.encode(), headers=headers)

    # ── Balance endpoints ─────────────────────────────────────────────────────

    async def _get_spot(self, wallet: ExchangeWallet) -> dict[str, Decimal]:
        r = await self._post(wallet, "/0/private/Balance")
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"Kraken Balance error: {data['error']}")
        out: dict[str, Decimal] = {}
        for raw_asset, amt in (data.get("result") or {}).items():
            asset = _norm(raw_asset)
            val = Decimal(str(amt))
            if val > 0:
                out[asset] = out.get(asset, Decimal(0)) + val
        return out

    async def _get_earn(self, wallet: ExchangeWallet) -> dict[str, Decimal]:
        """Kraken Earn allocations (staking, flexible earn)."""
        out: defaultdict = defaultdict(Decimal)
        cursor = None
        for _ in range(10):  # max 10 pages
            extra: dict = {}
            if cursor:
                extra["cursor"] = cursor
            try:
                r = await self._post(wallet, "/0/private/Earn/Allocations", extra)
                r.raise_for_status()
                data = r.json()
                if data.get("error"):
                    break
                result = data.get("result") or {}
                for item in result.get("items") or []:
                    asset = _norm(item.get("native_asset") or "")
                    amt = Decimal(str((item.get("amount_allocated") or {}).get("total") or "0"))
                    if asset and amt > 0:
                        out[asset] += amt
                if not result.get("next_cursor"):
                    break
                cursor = result["next_cursor"]
            except Exception:
                break
        return dict(out)

    # ── Main ─────────────────────────────────────────────────────────────────

    async def fetch_balance(self, wallet: ExchangeWallet) -> BalanceResult:
        spot_res, earn_res = await asyncio.gather(
            self._get_spot(wallet),
            self._get_earn(wallet),
            return_exceptions=True,
        )
        if isinstance(spot_res, Exception):
            raise spot_res
        earn = earn_res if isinstance(earn_res, dict) else {}
        return self._build_result(wallet, self.name, spot_res, {}, earn)
