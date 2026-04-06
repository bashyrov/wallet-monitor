from backend.providers.perp_dexes.aster_provider import AsterProvider
from backend.providers.perp_dexes.ethereal_provider import EtherealProvider
from backend.providers.perp_dexes.hyperliquid_provider import HyperliquidProvider
from backend.providers.perp_dexes.lighter_provider import LighterProvider
from backend.providers.perp_dexes.paradex_provider import ParadexProvider

PERPDEX_PROVIDERS = {
    "hyperliquid": HyperliquidProvider,
    "aster":       AsterProvider,
    "lighter":     LighterProvider,
    "ethereal":    EtherealProvider,
    "paradex":     ParadexProvider,
}
