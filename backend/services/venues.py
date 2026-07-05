"""Single source of truth for the public-facing venue lists. Other modules
already hold authoritative dictionaries (FETCHERS in arbitrage_service,
EXCHANGE_PROVIDERS / PERPDEX_PROVIDERS / CHAIN_META in providers); this
file just stitches them together so the frontend can render without
hard-coded "8 CEX, 5 perp DEX, 14 chains" copy that drifts every time
we add or drop a venue.

Exposed via GET /api/meta/venues — see backend/api/v1/meta.py.
"""

from __future__ import annotations

import time
import threading


# Simple in-process TTL cache. The meta result is deterministic per code
# state; only admin-DB writes (KEY_DISABLED_EXCHANGES, hidden symbols)
# change what the frontend sees — those call `invalidate_venues_cache()`
# via admin_settings.set_value so a toggle propagates within one request.
_CACHE_TTL_S = 60.0
_cache: dict = {}
_cache_expires_at: float = 0.0
_cache_lock = threading.Lock()


def invalidate_venues_cache() -> None:
    """Blow the memoized /api/meta/venues result. Called from
    admin_settings.set_value on any KEY_DISABLED_* / KEY_HIDDEN_* change
    so the frontend picks up an admin toggle without waiting 60s."""
    global _cache, _cache_expires_at
    with _cache_lock:
        _cache = {}
        _cache_expires_at = 0.0


def get_venues_meta() -> dict:
    global _cache, _cache_expires_at
    now = time.monotonic()
    with _cache_lock:
        if _cache and now < _cache_expires_at:
            return _cache
    fresh = _build_venues_meta()
    with _cache_lock:
        _cache = fresh
        _cache_expires_at = now + _CACHE_TTL_S
    return fresh


def _build_venues_meta() -> dict:
    """Return the live composition of supported venues — ids, labels,
    enabled flags, and the section headlines (counts) the marketing
    pages display."""
    # Lazy imports to avoid circulars at app start.
    from backend.services.arbitrage_service import FETCHERS
    from backend.providers.exchanges import EXCHANGE_PROVIDERS
    from backend.providers.perp_dexes import PERPDEX_PROVIDERS
    from backend.providers.chains import CHAIN_META
    from backend.services import admin_settings

    from backend.services.spot_arbitrage_service import SPOT_FETCHERS

    # DB-driven overrides. Any id in the admin's disabled lists is
    # stripped from what the frontend sees — same key backing the
    # /admin → Screener → Disabled exchanges chips.
    disabled_ex = admin_settings.get_disabled_exchanges()
    try:
        disabled_perpdex = admin_settings.get_disabled_perpdexes()
    except AttributeError:
        disabled_perpdex = set()
    try:
        disabled_chains = admin_settings.get_disabled_chains()
    except AttributeError:
        disabled_chains = set()

    def _drop_ex(ex: str) -> bool:
        return ex in disabled_ex

    SPOT_SCREENER_VENUES = {ex for ex in SPOT_FETCHERS.keys() if not _drop_ex(ex)}
    PERP_DEX_IDS = set(PERPDEX_PROVIDERS.keys())  # what we treat as perp-DEX
    SCREENER_PERP_DEX = {
        ex for ex in FETCHERS.keys()
        if (ex in PERP_DEX_IDS or ex == "extended") and ex not in disabled_perpdex
    }
    SCREENER_CEX = [
        ex for ex in FETCHERS.keys()
        if ex not in SCREENER_PERP_DEX and not _drop_ex(ex)
    ]

    def _label(provider_cls) -> str:
        return getattr(provider_cls, "label", None) or provider_cls.__name__

    portfolio_cex = [
        {
            "id": k,
            "label": _label(v),
            "enabled": getattr(v, "enabled", True),
            "soon": getattr(v, "soon", False),
        }
        for k, v in EXCHANGE_PROVIDERS.items()
        if getattr(v, "enabled", True) and k not in disabled_ex
    ]
    portfolio_perp_dex = [
        {
            "id": k,
            "label": _label(v),
            "enabled": getattr(v, "enabled", True),
            "soon": getattr(v, "soon", False),
        }
        for k, v in PERPDEX_PROVIDERS.items()
        if getattr(v, "enabled", True) and k not in disabled_perpdex
    ]
    chains = [
        {"id": k, "label": v.get("label", k), "enabled": v.get("enabled", True)}
        for k, v in CHAIN_META.items()
        if v.get("enabled", True) and k not in disabled_chains
    ]
    spot_screener = [
        {"id": ex, "label": _id_label(ex)}
        for ex in sorted(SPOT_SCREENER_VENUES)
    ]
    screener_cex = [
        {"id": ex, "label": _id_label(ex)} for ex in sorted(SCREENER_CEX)
    ]
    screener_perp_dex = [
        {"id": ex, "label": _id_label(ex)} for ex in sorted(SCREENER_PERP_DEX)
    ]

    return {
        "screener": {
            "cex": screener_cex,
            "perp_dex": screener_perp_dex,
            "spot": spot_screener,
        },
        "portfolio": {
            "cex": portfolio_cex,
            "perp_dex": portfolio_perp_dex,
            "chains": chains,
        },
        "counts": {
            "screener_cex": len(screener_cex),
            "screener_perp_dex": len(screener_perp_dex),
            "screener_spot": len(spot_screener),
            "portfolio_cex": len(portfolio_cex),
            "portfolio_perp_dex": len(portfolio_perp_dex),
            "portfolio_chains": len(chains),
        },
    }


# Human-readable labels for ids that don't have a provider class (screener-
# only venues like Extended live entirely in arbitrage_service.FETCHERS).
_FALLBACK_LABELS = {
    "binance": "Binance", "bybit": "Bybit", "okx": "OKX", "gate": "Gate",
    "kucoin": "KuCoin", "mexc": "MEXC", "bitget": "Bitget", "bingx": "BingX",
    "whitebit": "WhiteBIT", "htx": "HTX", "kraken": "Kraken",
    "backpack": "Backpack",
    "hyperliquid": "Hyperliquid", "aster": "Aster", "ethereal": "Ethereal",
    "paradex": "Paradex", "lighter": "Lighter", "extended": "Extended",
    "binancealpha": "Binance Alpha",
}


def _id_label(ex_id: str) -> str:
    """Best-effort human label for an exchange id."""
    try:
        from backend.providers.exchanges import EXCHANGE_PROVIDERS
        from backend.providers.perp_dexes import PERPDEX_PROVIDERS
        for table in (EXCHANGE_PROVIDERS, PERPDEX_PROVIDERS):
            cls = table.get(ex_id)
            if cls is not None:
                lbl = getattr(cls, "label", None)
                if lbl:
                    return lbl
    except Exception:
        pass
    return _FALLBACK_LABELS.get(ex_id, ex_id.title())
