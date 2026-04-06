from backend.providers.chains.evm_chains import EVMChainProvider
from backend.providers.chains.tron_provider import TronProvider

CHAIN_PROVIDERS = {
    # EVM chains (all handled by EVMChainProvider via Ankr API or per-chain RPC)
    "ethereum": EVMChainProvider,
    "bsc": EVMChainProvider,
    "polygon": EVMChainProvider,
    "arbitrum": EVMChainProvider,
    "optimism": EVMChainProvider,
    "base": EVMChainProvider,
    "avalanche": EVMChainProvider,
    "fantom": EVMChainProvider,
    "zksync": EVMChainProvider,
    "linea": EVMChainProvider,
    "scroll": EVMChainProvider,
    "mantle": EVMChainProvider,
    "blast": EVMChainProvider,
    "evm": EVMChainProvider,   # generic EVM fallback
    # Other chains
    "tron": TronProvider,
}

# UI metadata per chain (label + enabled flag).
# Chains not listed here or with enabled=False are hidden from the frontend.
CHAIN_META: dict[str, dict] = {
    "tron":      {"label": "Tron",      "enabled": True},
    "ethereum":  {"label": "Ethereum",  "enabled": True},
    "bsc":       {"label": "BSC",       "enabled": True},
    "polygon":   {"label": "Polygon",   "enabled": True},
    "arbitrum":  {"label": "Arbitrum",  "enabled": True},
    "optimism":  {"label": "Optimism",  "enabled": True},
    "base":      {"label": "Base",      "enabled": True},
    "avalanche": {"label": "Avalanche", "enabled": True},
    "zksync":    {"label": "zkSync",    "enabled": True},
    "linea":     {"label": "Linea",     "enabled": True},
    "scroll":    {"label": "Scroll",    "enabled": True},
    "mantle":    {"label": "Mantle",    "enabled": True},
    "blast":     {"label": "Blast",     "enabled": True},
    # fantom / evm — internal aliases, not shown in UI
}
