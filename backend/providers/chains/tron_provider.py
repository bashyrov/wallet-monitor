import asyncio
from collections import defaultdict
from decimal import Decimal

import httpx
from backend.providers.http import RetryClient

from backend.domain.models import BalanceResult
from backend.providers.chains.base_chain_provider import BaseChainProvider

TRX_DECIMALS = 6
MIN_DISPLAY_VALUE = Decimal("0.000001")

# Known TRC20 contracts: address -> (symbol, decimals)
KNOWN_TRC20: dict[str, tuple[str, int]] = {
    "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t": ("USDT", 6),
    "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8": ("USDC", 6),
    "TXpw8XeWYeTUd4quDskoUqeQPowRh4jY65": ("WBTC", 8),
    "TCFLL5dx5ZJdKnWuesXxi1VPwjLVmWZZy9": ("JST", 18),
    "TKfjV9RNKJJCqPvBtK8L7Knykh7DNWvnYt": ("WTRX", 6),
    "TF17BgPaZYbz8oxbjhriubPDsA7ArKoLX3": ("USDD", 18),
    "TAFjULxiVgT4qWk6UZwjqwZXTSaGaqnVp4": ("BTT", 6),
    "TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7": ("WIN", 6),
    "TNGRBpnfkNsxf5oFRNBWpFUEjm8HY5mMwC": ("SUN", 18),
    "TDkFBHSbFnNneDvBJ3vPPeLGEovkXV7kH6": ("WETH", 18),
    "TN3W4H6rK2ce4vX9YnFQHwKENnHjoxb3m9": ("WBNB", 18),
}

# Runtime cache so we don't re-query the same contract twice per process
_symbol_cache: dict[str, tuple[str, int]] = {}


async def _resolve_trc20_symbol(client: httpx.AsyncClient, contract: str) -> tuple[str, int]:
    """Resolve TRC20 token symbol and decimals via TronGrid API."""
    if contract in KNOWN_TRC20:
        return KNOWN_TRC20[contract]
    if contract in _symbol_cache:
        return _symbol_cache[contract]

    try:
        resp = await client.get(
            f"https://api.trongrid.io/v1/contracts/{contract}",
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json().get("data") or []
            if data:
                info = data[0]
                symbol = (info.get("token_abbr") or info.get("token_name") or contract[:8]).upper()
                decimals = int(info.get("token_decimal") or 6)
                _symbol_cache[contract] = (symbol, decimals)
                return symbol, decimals
    except Exception:
        pass

    fallback = (contract[:8].upper(), 6)
    _symbol_cache[contract] = fallback
    return fallback


class TronProvider(BaseChainProvider):
    name = "TronProvider"
    base_url = "https://api.trongrid.io"

    async def fetch_balance(self, wallet) -> BalanceResult:
        if not wallet.address:
            return BalanceResult(
                wallet=wallet, provider=self.name, totals={},
                details={"error": "Address is required"},
            )

        try:
            from settings import settings
            tron_key = settings.TRON_KEY
        except Exception:
            tron_key = None

        headers = {}
        if tron_key:
            headers["TRON-PRO-API-KEY"] = tron_key

        totals: dict[str, Decimal] = defaultdict(Decimal)

        try:
            async with RetryClient(timeout=20, headers=headers) as client:
                resp = await client.get(f"{self.base_url}/v1/accounts/{wallet.address}")
                resp.raise_for_status()
                payload = resp.json()

                data = payload.get("data") or []
                if not data:
                    return BalanceResult(
                        wallet=wallet, provider=self.name, totals={},
                        details={"assets": {}, "note": "Account not found or empty"},
                    )

                account = data[0]

                # TRX native balance (in SUN, 1 TRX = 10^6 SUN)
                balance_sun = account.get("balance", 0)
                trx = Decimal(balance_sun) / Decimal(10 ** TRX_DECIMALS)
                if trx >= MIN_DISPLAY_VALUE:
                    totals["TRX"] += trx

                # TRC20 tokens
                trc20_list = account.get("trc20") or []
                symbol_tasks = []
                entries: list[tuple[str, Decimal]] = []

                for item in trc20_list:
                    for contract, raw_value in item.items():
                        symbol_tasks.append(_resolve_trc20_symbol(client, contract))
                        entries.append((contract, Decimal(str(raw_value))))

                if symbol_tasks:
                    resolved = await asyncio.gather(*symbol_tasks, return_exceptions=True)
                    for (contract, raw_value), result in zip(entries, resolved):
                        if isinstance(result, Exception):
                            symbol, decimals = contract[:8].upper(), 6
                        else:
                            symbol, decimals = result
                        amount = raw_value / Decimal(10 ** decimals)
                        if amount >= MIN_DISPLAY_VALUE:
                            totals[symbol] += amount

        except Exception as e:
            return BalanceResult(
                wallet=wallet, provider=self.name, totals={},
                details={"error": f"TronProvider error: {e}"},
            )

        result = {k: str(v) for k, v in totals.items() if v > 0}
        return BalanceResult(
            wallet=wallet, provider=self.name, totals=result,
            details={"assets": result},
        )
