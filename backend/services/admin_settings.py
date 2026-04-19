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

_DEFAULTS: dict[str, Any] = {
    KEY_HIDDEN_SYMBOLS: [],
    KEY_DISABLED_EXCHANGES: [],
    KEY_MAINTENANCE: False,
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
