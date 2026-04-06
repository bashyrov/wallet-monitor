from collections import defaultdict
from decimal import Decimal

from backend.domain.models import BalanceResult
from backend.providers.chains.base_chain_provider import BaseChainProvider

MIN_DISPLAY_VALUE = Decimal("0.000001")

# Ankr multichain API blockchain names
ANKR_CHAIN_MAP: dict[str, str] = {
    "ethereum": "eth",
    "bsc": "bsc",
    "polygon": "polygon",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "base": "base",
    "avalanche": "avalanche",
    "fantom": "fantom",
    "zksync": "zksync_era",
    "linea": "linea",
    "scroll": "scroll",
    "mantle": "mantle",
    "blast": "blast",
}

# Native token symbol + decimals per chain
NATIVE_TOKEN: dict[str, tuple[str, int]] = {
    "ethereum": ("ETH", 18),
    "bsc": ("BNB", 18),
    "polygon": ("POL", 18),
    "arbitrum": ("ETH", 18),
    "optimism": ("ETH", 18),
    "base": ("ETH", 18),
    "avalanche": ("AVAX", 18),
    "fantom": ("FTM", 18),
    "zksync": ("ETH", 18),
    "linea": ("ETH", 18),
    "scroll": ("ETH", 18),
    "mantle": ("MNT", 18),
    "blast": ("ETH", 18),
}

# settings key name per chain (for plain-RPC fallback)
SETTINGS_RPC_KEY: dict[str, str] = {
    "ethereum": "ETHEREUM_RPC",
    "bsc": "BSC_RPC",
    "polygon": "POLYGON_RPC",
    "arbitrum": "ARBITRUM_RPC",
    "optimism": "OPTIMISM_RPC",
    "base": "BASE_RPC",
    "avalanche": "AVALANCHE_RPC",
    "fantom": "FANTOM_RPC",
    "zksync": "ZKSYNC_RPC",
    "linea": "LINEA_RPC",
    "scroll": "SCROLL_RPC",
    "mantle": "MANTLE_RPC",
    "blast": "BLAST_RPC",
}


class EVMChainProvider(BaseChainProvider):
    name = "EVMChainProvider"

    async def fetch_balance(self, wallet) -> BalanceResult:
        if not wallet.address:
            return BalanceResult(
                wallet=wallet, provider=self.name, totals={},
                details={"error": "Address is required"},
            )

        chain = wallet.chain.value if hasattr(wallet.chain, "value") else str(wallet.chain)

        try:
            from settings import settings
            ankr_key = settings.ANKR_KEY
        except Exception:
            ankr_key = None

        if ankr_key:
            return await self._fetch_via_ankr(wallet, chain, ankr_key)
        return await self._fetch_native_rpc(wallet, chain)

    async def _fetch_via_ankr(self, wallet, chain: str, ankr_key: str) -> BalanceResult:
        ankr_chain = ANKR_CHAIN_MAP.get(chain)
        if not ankr_chain:
            return BalanceResult(
                wallet=wallet, provider=self.name, totals={},
                details={"error": f"Chain '{chain}' not supported by Ankr"},
            )

        url = f"https://rpc.ankr.com/multichain/{ankr_key}"
        payload = {
            "jsonrpc": "2.0",
            "method": "ankr_getAccountBalance",
            "params": {
                "walletAddress": wallet.address,
                "blockchain": ankr_chain,
                "pageSize": 50,
            },
            "id": 1,
        }

        try:
            resp = await self._client.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return BalanceResult(
                wallet=wallet, provider=self.name, totals={},
                details={"error": f"Ankr request failed: {e}"},
            )

        if "error" in data:
            err = data["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            return BalanceResult(
                wallet=wallet, provider=self.name, totals={},
                details={"error": f"Ankr error: {msg}"},
            )

        assets = (data.get("result") or {}).get("assets") or []
        totals: dict[str, Decimal] = defaultdict(Decimal)

        for asset in assets:
            symbol = (asset.get("tokenSymbol") or "UNKNOWN").upper().strip()
            try:
                balance = Decimal(str(asset.get("balance") or "0"))
                if balance >= MIN_DISPLAY_VALUE:
                    totals[symbol] += balance
            except Exception:
                pass

        result = {k: str(v) for k, v in totals.items() if v > 0}
        return BalanceResult(
            wallet=wallet, provider=self.name, totals=result,
            details={"assets": result, "chain": chain, "source": "ankr"},
        )

    async def _fetch_native_rpc(self, wallet, chain: str) -> BalanceResult:
        from settings import settings

        rpc_key = SETTINGS_RPC_KEY.get(chain)
        rpc_url = getattr(settings, rpc_key, None) if rpc_key else None

        if not rpc_url:
            return BalanceResult(
                wallet=wallet, provider=self.name, totals={},
                details={"error": f"No RPC URL configured for chain '{chain}'. Set {rpc_key} in .env or provide ANKR_KEY."},
            )

        native_symbol, decimals = NATIVE_TOKEN.get(chain, ("ETH", 18))

        try:
            resp = await self._client.post(rpc_url, json={
                "jsonrpc": "2.0",
                "method": "eth_getBalance",
                "params": [wallet.address, "latest"],
                "id": 1,
            }, timeout=20)
            resp.raise_for_status()
            data = resp.json()

            hex_balance = data.get("result", "0x0") or "0x0"
            balance = Decimal(int(hex_balance, 16)) / Decimal(10 ** decimals)

            totals = {native_symbol: str(balance)} if balance >= MIN_DISPLAY_VALUE else {}
            return BalanceResult(
                wallet=wallet, provider=self.name, totals=totals,
                details={"assets": totals, "chain": chain, "source": "rpc", "note": "ERC-20 tokens require ANKR_KEY"},
            )
        except Exception as e:
            return BalanceResult(
                wallet=wallet, provider=self.name, totals={},
                details={"error": f"RPC error: {e}"},
            )
