from collections import defaultdict
from decimal import Decimal

import httpx
from backend.providers.http import RetryClient

from backend.domain.models import BalanceResult
from backend.providers.chains.base_chain_provider import BaseChainProvider

SOL_DECIMALS = 9
MIN_DISPLAY_VALUE = Decimal("0.000001")

# Public mainnet RPC as fallback
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"

# Hardcoded fallbacks for the most common tokens (avoids network round-trip)
KNOWN_SPL: dict[str, str] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "So11111111111111111111111111111111111111112":   "SOL",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "mSOL",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "ETH",
    "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E": "BTC",
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": "RAY",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN":  "JUP",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm": "WIF",
    "HZ1JovNiVvGrG9NWajT5QhqhKqfAmGNyaavbCJaGu3Ps": "PYTH",
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL":  "JTO",
    "MNDEFzGvMt87ueuHvVU9VcTqsAP5b3fTGPsHuuPA5ey":  "MNDE",
    "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux":  "HNT",
    "mb1eu7TzEc71KxDpsmsKoucSSuuoGLv1drys1oP2jh6":   "MOBILE",
    "SHDWyBxihqiCj6YekG2GUr7wqKLeLAMK1gHZck9pL6y":  "SHDW",
    "4vMsoUT2BWatFweudnQM1xedRLfJgJ7hswhcpz4xgBTy": "HAWK",
    "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr": "POPCAT",
    "A9mUU4qviSctJVPJdBJWkb28deg915LYJKrzQ19ji3FM":  "USDCE",
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1":  "bSOL",
}

# Module-level cache: mint → symbol, loaded once per process from Jupiter
_symbol_cache: dict[str, str] = dict(KNOWN_SPL)
_jupiter_loaded = False


async def _ensure_jupiter_cache(client: httpx.AsyncClient) -> None:
    """Load Jupiter strict token list into _symbol_cache (once per process)."""
    global _jupiter_loaded
    if _jupiter_loaded:
        return
    try:
        r = await client.get(
            "https://token.jup.ag/strict",
            timeout=15,
            headers={"Accept": "application/json"},
        )
        if r.status_code == 200:
            tokens = r.json()
            for t in tokens:
                mint = t.get("address")
                symbol = t.get("symbol")
                if mint and symbol:
                    _symbol_cache[mint] = symbol
        _jupiter_loaded = True
    except Exception:
        # Non-fatal: we still have KNOWN_SPL hardcoded
        _jupiter_loaded = True  # don't retry on every call


def _symbol_for(mint: str) -> str:
    return _symbol_cache.get(mint, mint[:8])


class SolanaProvider(BaseChainProvider):
    name = "SolanaProvider"

    def _rpc_url(self) -> str:
        try:
            from settings import settings
            return settings.SOLANA_RPC or DEFAULT_RPC
        except Exception:
            return DEFAULT_RPC

    async def fetch_balance(self, wallet) -> BalanceResult:
        if not wallet.address:
            return BalanceResult(
                wallet=wallet, provider=self.name, totals={},
                details={"error": "Address is required"},
            )

        address = wallet.address
        rpc = self._rpc_url()
        totals: dict[str, Decimal] = defaultdict(Decimal)

        # Populate token symbol cache from Jupiter (once)
        await _ensure_jupiter_cache(self._client)

        try:
            # SOL native balance
            resp = await self._client.post(rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getBalance",
                "params": [address],
            }, timeout=20)
            resp.raise_for_status()
            data = resp.json()

            lamports = (data.get("result") or {}).get("value", 0)
            sol = Decimal(lamports) / Decimal(10 ** SOL_DECIMALS)
            if sol >= MIN_DISPLAY_VALUE:
                totals["SOL"] += sol

            # SPL token accounts
            resp2 = await self._client.post(rpc, json={
                "jsonrpc": "2.0", "id": 2,
                "method": "getTokenAccountsByOwner",
                "params": [
                    address,
                    {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                    {"encoding": "jsonParsed"},
                ],
            }, timeout=20)
            resp2.raise_for_status()
            token_data = resp2.json()

            accounts = ((token_data.get("result") or {}).get("value") or [])
            for acc in accounts:
                info = (acc.get("account", {})
                           .get("data", {})
                           .get("parsed", {})
                           .get("info", {}))
                mint = info.get("mint", "")
                token_amount = info.get("tokenAmount", {})
                ui_amount = token_amount.get("uiAmountString") or token_amount.get("uiAmount")
                if ui_amount is None:
                    continue
                try:
                    amount = Decimal(str(ui_amount))
                except Exception:
                    continue
                if amount < MIN_DISPLAY_VALUE:
                    continue
                symbol = _symbol_for(mint)
                totals[symbol] += amount

        except Exception as e:
            return BalanceResult(
                wallet=wallet, provider=self.name, totals={},
                details={"error": f"Solana RPC error: {e}"},
            )

        result = {k: str(v) for k, v in totals.items() if v > 0}
        return BalanceResult(
            wallet=wallet, provider=self.name, totals=result,
            details={"assets": result},
        )
