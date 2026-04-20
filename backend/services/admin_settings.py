"""Admin-tunable runtime knobs (hidden tokens, disabled exchanges, etc.).

Values live in the `app_settings` table as JSON. Readers (arbitrage_service,
screener endpoints, etc.) hit an in-memory cache with a short TTL so the
hot path stays cheap; admin writes invalidate the cache so changes take
effect within a second.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from backend.db.base import SessionLocal
from backend.db.models import AppSetting

_TTL_S = 15.0
_lock = threading.Lock()
_cache: dict[str, tuple[Any, float]] = {}

KEY_HIDDEN_SYMBOLS = "hidden_symbols"
KEY_DISABLED_EXCHANGES = "disabled_exchanges"
KEY_MAINTENANCE = "maintenance_mode"
KEY_DISABLED_WALLET_EXCHANGES = "disabled_wallet_exchanges"
KEY_DISABLED_CHAINS = "disabled_chains"
KEY_DISABLED_PERPDEXES = "disabled_perpdexes"
KEY_SCREENER_DISABLED = "screener_disabled"
KEY_PORTFOLIO_DISABLED = "portfolio_disabled"
KEY_TRADE_DISABLED_EXCHANGES = "trade_disabled_exchanges"
KEY_ARB_MIN_VOLUME_USD = "arb_min_volume_usd"
KEY_ARB_EXCLUDE_EXCHANGES = "arb_exclude_exchanges"

_DEFAULTS: dict[str, Any] = {
    KEY_HIDDEN_SYMBOLS: [],
    KEY_DISABLED_EXCHANGES: [],
    KEY_MAINTENANCE: False,
    KEY_DISABLED_WALLET_EXCHANGES: [],
    KEY_DISABLED_CHAINS: [],
    KEY_DISABLED_PERPDEXES: [],
    KEY_SCREENER_DISABLED: False,
    KEY_PORTFOLIO_DISABLED: False,
    KEY_TRADE_DISABLED_EXCHANGES: [],
    # Pair considered tradeable only if EITHER leg's 24h USD volume
    # clears this bar. Raise to hide thin listings; lower to surface
    # more obscure arbs at the cost of possibly-fake opportunities
    # from low-liquidity feeds.
    KEY_ARB_MIN_VOLUME_USD: 100_000,
    # Exchanges excluded from arb pair computation (still visible on
    # funding / portfolio sides). Historically we've excluded Kraken
    # for spread-quality reasons.
    KEY_ARB_EXCLUDE_EXCHANGES: ["kraken"],
}


def _load(key: str) -> Any:
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        return row.value if row else _DEFAULTS.get(key)
    finally:
        db.close()


def get(key: str) -> Any:
    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry and now - entry[1] < _TTL_S:
            return entry[0]
    value = _load(key)
    with _lock:
        _cache[key] = (value, now)
    return value


def set_value(key: str, value: Any, user_id: int | None = None) -> None:
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row:
            row.value = value
            row.updated_by = user_id
        else:
            db.add(AppSetting(key=key, value=value, updated_by=user_id))
        db.commit()
    finally:
        db.close()
    with _lock:
        _cache.pop(key, None)


def get_hidden_symbols() -> set[str]:
    v = get(KEY_HIDDEN_SYMBOLS) or []
    return {str(s).upper() for s in v}


def get_disabled_exchanges() -> set[str]:
    v = get(KEY_DISABLED_EXCHANGES) or []
    return {str(s).lower() for s in v}


def is_maintenance() -> bool:
    return bool(get(KEY_MAINTENANCE))


def is_screener_disabled() -> bool:
    return bool(get(KEY_SCREENER_DISABLED))


def is_portfolio_disabled() -> bool:
    return bool(get(KEY_PORTFOLIO_DISABLED))


def _as_lower_set(key: str) -> set[str]:
    return {str(s).lower() for s in (get(key) or [])}


def get_disabled_wallet_exchanges() -> set[str]:
    return _as_lower_set(KEY_DISABLED_WALLET_EXCHANGES)


def get_disabled_chains() -> set[str]:
    return _as_lower_set(KEY_DISABLED_CHAINS)


def get_disabled_perpdexes() -> set[str]:
    return _as_lower_set(KEY_DISABLED_PERPDEXES)


def get_trade_disabled_exchanges() -> set[str]:
    """Exchanges where the admin has blocked users from opening new
    positions through our service (the exchange stays visible on the
    screener/funding/portfolio sides)."""
    return _as_lower_set(KEY_TRADE_DISABLED_EXCHANGES)


def get_arb_min_volume_usd() -> float:
    """Minimum 24h USD volume for a symbol to be considered tradeable
    in the arb engine. Defaults to $100K; admin-tunable."""
    v = get(KEY_ARB_MIN_VOLUME_USD)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(_DEFAULTS[KEY_ARB_MIN_VOLUME_USD])


def get_arb_exclude_exchanges() -> set[str]:
    return _as_lower_set(KEY_ARB_EXCLUDE_EXCHANGES)
