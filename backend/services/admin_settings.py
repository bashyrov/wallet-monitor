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
KEY_EXPIRY_NOTICE_DAYS = "expiry_notice_days"
KEY_EXPIRY_NOTICE_INTERVAL_HOURS = "expiry_notice_interval_hours"
# Minimum payout-request amount in USD. The user can submit only when
# their available balance ≥ this floor; lower amounts get rejected with
# "Minimum payout is $X". Admin tunes via /admin → Communications.
KEY_REFERRAL_MIN_PAYOUT_USD = "referral_min_payout_usd"
# Maintenance ETAs — ISO strings stored alongside the boolean flags. When
# set, the lockout page renders a countdown + "ends at HH:MM <TZ>" string.
KEY_MAINTENANCE_ENDS_AT = "maintenance_ends_at"
KEY_SCREENER_DISABLED_ENDS_AT = "screener_disabled_ends_at"
KEY_PORTFOLIO_DISABLED_ENDS_AT = "portfolio_disabled_ends_at"
# Display TZ for maintenance pages. IANA name, e.g. Europe/Warsaw.
KEY_MAINTENANCE_TZ = "maintenance_tz"
# Site-wide announcement banner (top of every page, above the navbar).
# Three fields: enabled toggle, plain-text message, marquee on/off.
KEY_BANNER_ENABLED = "banner_enabled"
KEY_BANNER_TEXT = "banner_text"
KEY_BANNER_MARQUEE = "banner_marquee"

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
    # Global min 24h USD volume floor. Applied at the data layer in
    # get_funding_data: ANY row with volume below this threshold — or
    # with missing/zero volume — is dropped from every downstream view
    # (funding tab, arb tab, alerts). Change via /api/admin/screener-config
    # without a deploy.
    KEY_ARB_MIN_VOLUME_USD: 50_000,
    # Exchanges excluded from arb pair computation (still visible on
    # funding / portfolio sides). Historically we've excluded Kraken
    # for spread-quality reasons.
    KEY_ARB_EXCLUDE_EXCHANGES: ["kraken"],
    # Expiry notification policy. Reminder fires when plan_expires_at is
    # less than `days` away, then re-fires every `interval_hours` until
    # expiry (or until the user cancels auto_renew, in which case we go
    # silent). 3 days × 24 h = "ping every morning starting 3 days before".
    KEY_EXPIRY_NOTICE_DAYS: 3,
    KEY_EXPIRY_NOTICE_INTERVAL_HOURS: 24,
    # Default $100 — covers TRC20 network fee + keeps the admin queue
    # meaningful. Floor 1, ceiling 10000 to prevent typos locking
    # everyone out.
    KEY_REFERRAL_MIN_PAYOUT_USD: 100,
    KEY_MAINTENANCE_ENDS_AT: None,
    KEY_SCREENER_DISABLED_ENDS_AT: None,
    KEY_PORTFOLIO_DISABLED_ENDS_AT: None,
    # Default to Poland (Warsaw). Owner is in PL/UA timezones; users see this
    # as "Tech work ends at 13:00 (Europe/Warsaw)".
    KEY_MAINTENANCE_TZ: "Europe/Warsaw",
    KEY_BANNER_ENABLED: False,
    KEY_BANNER_TEXT: "",
    KEY_BANNER_MARQUEE: False,
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


def get_expiry_notice_days() -> int:
    """How many days before plan_expires_at the notifier starts pinging
    the user. Hard floor 0 (disables notices), ceiling 60 (anything beyond
    is spammy)."""
    try:
        v = int(get(KEY_EXPIRY_NOTICE_DAYS) or 0)
    except (TypeError, ValueError):
        v = int(_DEFAULTS[KEY_EXPIRY_NOTICE_DAYS])
    return max(0, min(60, v))


def get_expiry_notice_interval_hours() -> int:
    """Hours between consecutive expiry reminders for the same user. Floor
    1 h, ceiling 168 h (one week)."""
    try:
        v = int(get(KEY_EXPIRY_NOTICE_INTERVAL_HOURS) or 0)
    except (TypeError, ValueError):
        v = int(_DEFAULTS[KEY_EXPIRY_NOTICE_INTERVAL_HOURS])
    return max(1, min(168, v))


def get_referral_min_payout_usd() -> float:
    """Minimum payout-request amount. Below this, the user can earn but
    can't withdraw — keeps TRC20 fees from eating the whole transfer and
    keeps the admin queue manageable. Floor $1 / ceiling $10k to defend
    against typos."""
    try:
        v = float(get(KEY_REFERRAL_MIN_PAYOUT_USD) or 0)
    except (TypeError, ValueError):
        v = float(_DEFAULTS[KEY_REFERRAL_MIN_PAYOUT_USD])
    return max(1.0, min(10000.0, v))


def get_maintenance_tz() -> str:
    v = (get(KEY_MAINTENANCE_TZ) or "").strip()
    return v or "Europe/Warsaw"


def _ends_at(key: str) -> str | None:
    """Read an ISO datetime string from settings, drop it if it's already
    in the past so stale ETAs don't mislead users."""
    raw = (get(key) or "")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt <= datetime.now(timezone.utc):
            return None
        return dt.isoformat()
    except (ValueError, TypeError):
        return None


def get_maintenance_ends_at() -> str | None:
    return _ends_at(KEY_MAINTENANCE_ENDS_AT)


def get_screener_disabled_ends_at() -> str | None:
    return _ends_at(KEY_SCREENER_DISABLED_ENDS_AT)


def get_portfolio_disabled_ends_at() -> str | None:
    return _ends_at(KEY_PORTFOLIO_DISABLED_ENDS_AT)


def get_banner() -> dict:
    """Site-wide announcement banner — public via /api/banner. Empty/disabled
    state returns enabled=False so the JS loader removes the banner element
    without flicker."""
    return {
        "enabled": bool(get(KEY_BANNER_ENABLED)),
        "text": str(get(KEY_BANNER_TEXT) or "").strip(),
        "marquee": bool(get(KEY_BANNER_MARQUEE)),
    }
