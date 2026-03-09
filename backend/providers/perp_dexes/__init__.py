from backend.providers.perp_dexes.ethereal_provider import EtherealProvider
from backend.providers.perp_dexes.hyperliquid_provider import HyperliquidProvider
from backend.providers.perp_dexes.lighter_provider import LighterProvider

PERPDEX_PROVIDERS = {
    "lighter": LighterProvider,
    "dydx": "DydxProvider",
    "hyperliquid": HyperliquidProvider,
    "ethereal": EtherealProvider,
    "paradex": "ParadexProvider",
    "extended": "ExtendedProvider",
    "nado": "NadoProvider"
}
