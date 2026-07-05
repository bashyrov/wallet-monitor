"""Binance Alpha wallet provider.

Alpha is a view over the user's regular Binance spot account — tokens
bought through the Alpha UI land in the ordinary spot wallet. Same API
key/secret as a plain Binance wallet; we filter spot balances down to
the Alpha token list and feed Alpha's USD prices into the shared price
cache (these long-tail tokens are never in the CMC/Gate top-100 sweep).
"""
import logging
import time
from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlencode

from backend.domain import ExchangeWallet
from backend.domain.models import BalanceResult
from backend.providers.base_wallet_provider import BaseWalletProvider
from backend.providers.exchanges._signing import ms, hex_hmac_sha256
from backend.providers.http import RetryClient

logger = logging.getLogger("avalant.providers.binancealpha")

ALPHA_TOKEN_LIST_URL = (
    "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
)
SPOT_BASE = "https://api.binance.com"

_ALPHA_TTL_S = 300.0
_alpha_cache: tuple[dict[str, dict], float] = ({}, 0.0)


async def get_alpha_tokens(http) -> dict[str, dict]:
    """Symbol → {price, volume_usd, liquidity_usd} for every live Alpha
    listing. Cached 5 min (same cadence as go-fetcher's binancealpha
    service). Multi-chain listings collapse to the deepest-liquidity
    placement. Serves stale data on fetch failure."""
    global _alpha_cache
    tokens, ts = _alpha_cache
    if tokens and (time.time() - ts) < _ALPHA_TTL_S:
        return tokens
    try:
        r = await http.get(ALPHA_TOKEN_LIST_URL, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        doc = r.json()
        fresh: dict[str, dict] = {}
        for t in doc.get("data") or []:
            sym = (t.get("symbol") or "").strip().upper()
            if not sym or t.get("fullyDelisted") or not t.get("contractAddress"):
                continue
            try:
                px = float(t.get("price") or 0)
                liq = float(t.get("liquidity") or 0)
                vol = float(t.get("volume24h") or 0)
            except (TypeError, ValueError):
                continue
            if px <= 0:
                continue
            prev = fresh.get(sym)
            if prev is None or liq > prev["liquidity_usd"]:
                fresh[sym] = {"price": px, "volume_usd": vol, "liquidity_usd": liq}
        if fresh:
            _alpha_cache = (fresh, time.time())
            return fresh
    except Exception as exc:
        logger.warning("alpha token list fetch failed: %s", exc)
    return tokens


class BinanceAlphaProvider(BaseWalletProvider):
    name = "BinanceAlphaProvider"
    label = "Binance Alpha"
    enabled = True
    needs_passphrase = False
    needs_api_key = True
    base_url = SPOT_BASE

    def __init__(self) -> None:
        self._http = RetryClient(timeout=20)

    async def aclose(self) -> None:
        await self._http.aclose()

    def creds_execution(self, wallet: ExchangeWallet) -> dict[str, str]:
        if not wallet.api_key or not wallet.api_secret:
            raise ValueError("BINANCE ALPHA api_key/api_secret are required")
        return {
            "api_key": wallet.api_key.strip(),
            "api_secret": wallet.api_secret.strip(),
        }

    async def _spot_account(self, creds: dict[str, str]) -> dict:
        params = {"timestamp": int(ms()), "recvWindow": 5000}
        qs = urlencode(params, doseq=True)
        sig = hex_hmac_sha256(creds["api_secret"], qs)
        r = await self._http.get(
            f"{self.base_url}/api/v3/account?{qs}&signature={sig}",
            headers={"X-MBX-APIKEY": creds["api_key"]},
        )
        r.raise_for_status()
        return r.json()

    async def fetch_balance(self, wallet: ExchangeWallet) -> BalanceResult:
        creds = self.creds_execution(wallet)
        try:
            account = await self._spot_account(creds)
            tokens = await get_alpha_tokens(self._http)

            spot: defaultdict = defaultdict(Decimal)
            for b in account.get("balances") or []:
                asset = (b.get("asset") or "").strip().upper()
                if asset not in tokens:
                    continue
                amt = Decimal(str(b.get("free") or "0")) + Decimal(str(b.get("locked") or "0"))
                if amt != 0:
                    spot[asset] += amt

            if tokens:
                from backend.services.price_service import merge_extra_prices
                merge_extra_prices({s: t["price"] for s, t in tokens.items()})

            return self._build_result(wallet, self.name, dict(spot), {}, {})
        finally:
            await self.aclose()
