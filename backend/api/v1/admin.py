import os
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.api.deps import get_admin_user, get_db
from backend.db.models import User, Wallet, Tag, ProviderErrorLog
from backend.plans import PLAN_LIMITS, VALID_PLANS, ADMIN_ONLY_PLANS, wallet_limit
from backend.services import admin_settings, audit_log
from backend.services.arbitrage_service import FETCHERS

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/stats")
def admin_stats(
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    users_count    = db.query(func.count(User.id)).scalar()
    wallets_count  = db.query(func.count(Wallet.id)).scalar()
    tags_count     = db.query(func.count(Tag.id)).scalar()
    requests_total = db.query(func.sum(User.request_count)).scalar() or 0

    rows = (
        db.query(Wallet.wallet_type, Wallet.type_value, func.count(Wallet.id))
        .group_by(Wallet.wallet_type, Wallet.type_value)
        .all()
    )
    by_type: dict[str, dict] = {}
    for wtype, tval, cnt in rows:
        if wtype not in by_type:
            by_type[wtype] = {"count": 0, "providers": {}}
        by_type[wtype]["count"] += cnt
        by_type[wtype]["providers"][tval] = cnt

    recent = (
        db.query(User)
        .order_by(User.created_at.desc())
        .limit(10)
        .all()
    )
    recent_users = []
    for u in recent:
        wc = db.query(func.count(Wallet.id)).filter(Wallet.user_id == u.id).scalar()
        recent_users.append({
            "username": u.username,
            "email": u.email,
            "is_admin": u.is_admin,
            "wallets": wc,
            "last_active_at": u.last_active_at.strftime("%Y-%m-%d %H:%M") if u.last_active_at else None,
            "joined": u.created_at.strftime("%Y-%m-%d %H:%M"),
        })

    return {
        "users_count": users_count,
        "wallets_count": wallets_count,
        "tags_count": tags_count,
        "requests_total": requests_total,
        "by_type": by_type,
        "recent_users": recent_users,
    }


@router.get("/users")
def admin_list_users(
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    users = db.query(User).order_by(User.created_at).all()
    result = []
    for u in users:
        wc = db.query(func.count(Wallet.id)).filter(Wallet.user_id == u.id).scalar()
        last_active = u.last_active_at.strftime("%Y-%m-%d %H:%M") if u.last_active_at else None
        plan = getattr(u, 'plan', 'basic') or 'basic'
        expires = u.plan_expires_at.strftime("%Y-%m-%d") if getattr(u, 'plan_expires_at', None) else None
        result.append({
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "is_admin": u.is_admin,
            "is_blocked": getattr(u, 'is_blocked', False),
            "plan": plan,
            "plan_expires_at": expires,
            "wallet_limit": wallet_limit(plan),
            "request_count": getattr(u, 'request_count', 0),
            "last_active_at": last_active,
            "wallets": wc,
            "created_at": u.created_at.strftime("%Y-%m-%d %H:%M"),
        })
    return result


@router.get("/users/{user_id}")
def admin_get_user(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    wallets = db.query(Wallet).filter(Wallet.user_id == user_id).all()
    plan = getattr(user, 'plan', 'basic') or 'basic'
    expires = user.plan_expires_at.strftime("%Y-%m-%d") if getattr(user, 'plan_expires_at', None) else None
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "is_admin": user.is_admin,
        "is_blocked": getattr(user, "is_blocked", False),
        "plan": plan,
        "plan_expires_at": expires,
        "wallet_limit": wallet_limit(plan),
        "request_count": getattr(user, "request_count", 0),
        "last_active_at": user.last_active_at.strftime("%Y-%m-%d %H:%M") if user.last_active_at else None,
        "created_at": user.created_at.strftime("%Y-%m-%d %H:%M"),
        "wallets": [
            {"id": w.id, "name": w.name, "wallet_type": w.wallet_type, "type_value": w.type_value}
            for w in wallets
        ],
    }



@router.get("/provider-errors")
def provider_errors(
    n: int = Query(default=500, ge=1, le=10000),
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Return error counts grouped by provider, from the last N error rows."""
    subq = (
        db.query(ProviderErrorLog)
        .order_by(ProviderErrorLog.created_at.desc())
        .limit(n)
        .subquery()
    )
    rows = (
        db.query(
            subq.c.wallet_type,
            subq.c.type_value,
            subq.c.error_type,
            func.count().label("count"),
            func.max(subq.c.created_at).label("last_seen"),
        )
        .group_by(subq.c.wallet_type, subq.c.type_value, subq.c.error_type)
        .order_by(func.count().desc())
        .all()
    )
    total = db.query(func.count(ProviderErrorLog.id)).scalar() or 0
    return {
        "window": n,
        "total_stored": total,
        "rows": [
            {
                "wallet_type": r.wallet_type,
                "type_value":  r.type_value,
                "error_type":  r.error_type,
                "count":       r.count,
                "last_seen":   r.last_seen.strftime("%Y-%m-%d %H:%M") if r.last_seen else None,
            }
            for r in rows
        ],
    }


@router.patch("/users/{user_id}/block")
def toggle_block(
    user_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    if user_id == current_admin.id:
        raise HTTPException(status_code=400, detail="Cannot block yourself")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_blocked = not getattr(user, 'is_blocked', False)
    # Unblock also clears the failed-login counter — otherwise a user
    # who was auto-locked at 5 failures would re-lock on their first
    # mistype after the admin's unblock.
    if not user.is_blocked:
        user.failed_login_attempts = 0
    db.commit()
    from backend.services.auth_cache import invalidate_user
    invalidate_user(user.id)
    if user.is_blocked:
        try:
            from backend.services.admin_alert_service import alert_user_blocked
            alert_user_blocked(user, f"by admin {current_admin.username}")
        except Exception:
            pass
    return {"id": user.id, "username": user.username, "is_blocked": user.is_blocked}


from pydantic import BaseModel
from datetime import datetime as _dt


class _PlanBody(BaseModel):
    plan: str
    plan_expires_at: str | None = None


@router.patch("/users/{user_id}/plan")
def set_plan(
    user_id: int,
    body: _PlanBody,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    if body.plan not in VALID_PLANS:
        raise HTTPException(status_code=400, detail=f"Invalid plan. Valid: {', '.join(sorted(VALID_PLANS))}")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if body.plan in ADMIN_ONLY_PLANS and not user.is_admin:
        raise HTTPException(status_code=400, detail="Plan 'unlim' can only be assigned to admin users")

    # Resolve the slug to a real Plan row — `user.plan_id` is the source of
    # truth for plan_service.get_user_plan(); the legacy string column was
    # being written alone, leaving plan_id stale and silently no-oping every
    # admin plan change. Fix: update both, prefer plan_id semantics.
    from backend.db.models import Plan
    plan_row = db.query(Plan).filter(Plan.slug == body.plan).first()
    if not plan_row:
        raise HTTPException(status_code=400, detail=f"Plan slug '{body.plan}' not in DB")

    user.plan = body.plan          # legacy string — kept for old serializers
    user.plan_id = plan_row.id     # source of truth for limits

    if body.plan_expires_at:
        try:
            user.plan_expires_at = _dt.strptime(body.plan_expires_at, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="plan_expires_at must be YYYY-MM-DD")
    else:
        user.plan_expires_at = None
    db.commit()

    # Invalidate the auth cache so the next /me read sees the new plan.
    from backend.services.auth_cache import invalidate_user
    invalidate_user(user.id)

    # Auto-archive surplus wallets if the new plan has a smaller portfolio
    # quota — without this, a downgrade leaves the user above the cap until
    # their next /me hit and any new-wallet attempt 402s confusingly.
    try:
        from backend.services import wallet_quota
        wallet_quota.enforce_for_user(db, user)
    except Exception as exc:
        logger.warning("wallet_quota.enforce_for_user failed for user_id=%s: %s", user.id, exc)

    plan = user.plan
    expires = user.plan_expires_at.strftime("%Y-%m-%d") if user.plan_expires_at else None
    return {
        "id": user.id,
        "username": user.username,
        "plan": plan,
        "plan_expires_at": expires,
        "wallet_limit": wallet_limit(plan),
    }


# ═══ Screener runtime controls ════════════════════════════════════════════════

class ScreenerConfigIn(BaseModel):
    hidden_symbols: list[str] | None = None
    disabled_exchanges: list[str] | None = None
    maintenance_mode: bool | None = None
    screener_disabled: bool | None = None
    portfolio_disabled: bool | None = None
    trade_disabled_exchanges: list[str] | None = None
    arb_min_volume_usd: float | None = None
    arb_exclude_exchanges: list[str] | None = None
    expiry_notice_days: int | None = None
    expiry_notice_interval_hours: int | None = None
    # Maintenance ETAs — ISO datetime (UTC) or null to clear. Admin can also
    # pass duration_minutes via the dedicated /maintenance endpoint.
    maintenance_ends_at: str | None = None
    screener_disabled_ends_at: str | None = None
    portfolio_disabled_ends_at: str | None = None
    maintenance_tz: str | None = None


def _trade_supported_set() -> set[str]:
    from backend.services.trade_adapters import TRADE_SUPPORTED
    return set(TRADE_SUPPORTED)


@router.get("/screener-config")
def screener_config_get(_: User = Depends(get_admin_user)):
    return {
        "hidden_symbols": sorted(admin_settings.get_hidden_symbols()),
        "disabled_exchanges": sorted(admin_settings.get_disabled_exchanges()),
        "available_exchanges": sorted(FETCHERS.keys()),
        "maintenance_mode": admin_settings.is_maintenance(),
        "screener_disabled": admin_settings.is_screener_disabled(),
        "portfolio_disabled": admin_settings.is_portfolio_disabled(),
        "trade_disabled_exchanges": sorted(admin_settings.get_trade_disabled_exchanges()),
        "trade_supported_exchanges": sorted(_trade_supported_set()),
        "arb_min_volume_usd": admin_settings.get_arb_min_volume_usd(),
        "arb_exclude_exchanges": sorted(admin_settings.get_arb_exclude_exchanges()),
        "expiry_notice_days": admin_settings.get_expiry_notice_days(),
        "expiry_notice_interval_hours": admin_settings.get_expiry_notice_interval_hours(),
        "maintenance_ends_at": admin_settings.get_maintenance_ends_at(),
        "screener_disabled_ends_at": admin_settings.get_screener_disabled_ends_at(),
        "portfolio_disabled_ends_at": admin_settings.get_portfolio_disabled_ends_at(),
        "maintenance_tz": admin_settings.get_maintenance_tz(),
    }


@router.patch("/screener-config")
def screener_config_patch(
    body: ScreenerConfigIn,
    user: User = Depends(get_admin_user),
):
    known_ex = set(FETCHERS.keys())
    if body.hidden_symbols is not None:
        cleaned = sorted({str(s).strip().upper() for s in body.hidden_symbols if str(s).strip()})
        admin_settings.set_value(admin_settings.KEY_HIDDEN_SYMBOLS, cleaned, user_id=user.id)
    if body.disabled_exchanges is not None:
        cleaned_ex = sorted({
            str(s).strip().lower() for s in body.disabled_exchanges
            if str(s).strip().lower() in known_ex
        })
        admin_settings.set_value(admin_settings.KEY_DISABLED_EXCHANGES, cleaned_ex, user_id=user.id)
    if body.maintenance_mode is not None:
        admin_settings.set_value(admin_settings.KEY_MAINTENANCE, bool(body.maintenance_mode), user_id=user.id)
    if body.screener_disabled is not None:
        admin_settings.set_value(admin_settings.KEY_SCREENER_DISABLED, bool(body.screener_disabled), user_id=user.id)
    if body.portfolio_disabled is not None:
        admin_settings.set_value(admin_settings.KEY_PORTFOLIO_DISABLED, bool(body.portfolio_disabled), user_id=user.id)
    if body.trade_disabled_exchanges is not None:
        known = _trade_supported_set()
        cleaned = sorted({
            str(s).strip().lower() for s in body.trade_disabled_exchanges
            if str(s).strip().lower() in known
        })
        admin_settings.set_value(admin_settings.KEY_TRADE_DISABLED_EXCHANGES, cleaned, user_id=user.id)
    if body.arb_min_volume_usd is not None:
        v = max(0.0, float(body.arb_min_volume_usd))
        admin_settings.set_value(admin_settings.KEY_ARB_MIN_VOLUME_USD, v, user_id=user.id)
    if body.arb_exclude_exchanges is not None:
        cleaned = sorted({
            str(s).strip().lower() for s in body.arb_exclude_exchanges
            if str(s).strip().lower() in known_ex
        })
        admin_settings.set_value(admin_settings.KEY_ARB_EXCLUDE_EXCHANGES, cleaned, user_id=user.id)
    if body.expiry_notice_days is not None:
        v = max(0, min(60, int(body.expiry_notice_days)))
        admin_settings.set_value(admin_settings.KEY_EXPIRY_NOTICE_DAYS, v, user_id=user.id)
    if body.expiry_notice_interval_hours is not None:
        v = max(1, min(168, int(body.expiry_notice_interval_hours)))
        admin_settings.set_value(admin_settings.KEY_EXPIRY_NOTICE_INTERVAL_HOURS, v, user_id=user.id)
    # Each ETA is a JSON-passable ISO string ("2026-04-26T13:00:00+00:00")
    # or empty string / null to clear. The admin can also use the dedicated
    # POST /admin/maintenance endpoint below to set "kick off + duration"
    # in a single round-trip.
    for body_field, key in (
        ("maintenance_ends_at",          admin_settings.KEY_MAINTENANCE_ENDS_AT),
        ("screener_disabled_ends_at",    admin_settings.KEY_SCREENER_DISABLED_ENDS_AT),
        ("portfolio_disabled_ends_at",   admin_settings.KEY_PORTFOLIO_DISABLED_ENDS_AT),
    ):
        v = getattr(body, body_field, None)
        if v is None:
            continue
        v = (v or "").strip() or None
        admin_settings.set_value(key, v, user_id=user.id)
    if body.maintenance_tz is not None:
        tz = (body.maintenance_tz or "").strip() or "Europe/Warsaw"
        admin_settings.set_value(admin_settings.KEY_MAINTENANCE_TZ, tz, user_id=user.id)
    return screener_config_get(user)


# Convenience: flip a maintenance scope on with a duration in minutes,
# computed server-side so we don't ship `now()` to the client. POST so
# accidental refreshes don't re-trigger.
class _MaintenanceBody(BaseModel):
    scope: str  # "site" | "screener" | "portfolio"
    enabled: bool
    duration_minutes: int | None = None
    tz: str | None = None


@router.post("/maintenance")
def maintenance_kick(
    body: _MaintenanceBody,
    user: User = Depends(get_admin_user),
):
    from datetime import datetime, timezone, timedelta
    scope = (body.scope or "").strip().lower()
    if scope not in ("site", "screener", "portfolio"):
        raise HTTPException(status_code=400, detail="scope must be 'site' | 'screener' | 'portfolio'")
    flag_key, ends_key = {
        "site":      (admin_settings.KEY_MAINTENANCE,         admin_settings.KEY_MAINTENANCE_ENDS_AT),
        "screener":  (admin_settings.KEY_SCREENER_DISABLED,   admin_settings.KEY_SCREENER_DISABLED_ENDS_AT),
        "portfolio": (admin_settings.KEY_PORTFOLIO_DISABLED,  admin_settings.KEY_PORTFOLIO_DISABLED_ENDS_AT),
    }[scope]
    admin_settings.set_value(flag_key, bool(body.enabled), user_id=user.id)
    if body.enabled and body.duration_minutes and body.duration_minutes > 0:
        ends = datetime.now(timezone.utc) + timedelta(minutes=int(body.duration_minutes))
        admin_settings.set_value(ends_key, ends.isoformat(), user_id=user.id)
    elif not body.enabled:
        admin_settings.set_value(ends_key, None, user_id=user.id)
    if body.tz:
        admin_settings.set_value(admin_settings.KEY_MAINTENANCE_TZ, body.tz, user_id=user.id)
    return screener_config_get(user)


# ═══ Site-wide announcement banner ════════════════════════════════════════════

class _BannerBody(BaseModel):
    enabled: bool | None = None
    text: str | None = None
    marquee: bool | None = None


@router.get("/banner")
def admin_banner_get(_: User = Depends(get_admin_user)):
    return admin_settings.get_banner()


@router.patch("/banner")
def admin_banner_patch(body: _BannerBody, user: User = Depends(get_admin_user)):
    """Admin: toggle the site-wide banner, set its text, switch between static
    and marquee. Cap the text at 500 chars so a fat-finger paste can't
    blow up every page."""
    if body.text is not None:
        text = (body.text or "").strip()[:500]
        admin_settings.set_value(admin_settings.KEY_BANNER_TEXT, text, user_id=user.id)
    if body.enabled is not None:
        admin_settings.set_value(admin_settings.KEY_BANNER_ENABLED, bool(body.enabled), user_id=user.id)
    if body.marquee is not None:
        admin_settings.set_value(admin_settings.KEY_BANNER_MARQUEE, bool(body.marquee), user_id=user.id)
    return admin_settings.get_banner()


# ═══ Portfolio runtime controls ═══════════════════════════════════════════════

class PortfolioConfigIn(BaseModel):
    disabled_wallet_exchanges: list[str] | None = None
    disabled_chains: list[str] | None = None
    disabled_perpdexes: list[str] | None = None


def _portfolio_inventory() -> dict:
    from backend.providers.exchanges import EXCHANGE_PROVIDERS
    from backend.providers.perp_dexes import PERPDEX_PROVIDERS
    from backend.providers.chains import CHAIN_META
    return {
        "available_wallet_exchanges": sorted(
            v for v, p in EXCHANGE_PROVIDERS.items()
            if isinstance(p, type) and getattr(p, "enabled", True)
        ),
        "available_chains": sorted(
            v for v, m in CHAIN_META.items() if m.get("enabled", True)
        ),
        "available_perpdexes": sorted(
            v for v, p in PERPDEX_PROVIDERS.items()
            if isinstance(p, type) and getattr(p, "enabled", True)
        ),
    }


@router.get("/portfolio-config")
def portfolio_config_get(_: User = Depends(get_admin_user)):
    return {
        **_portfolio_inventory(),
        "disabled_wallet_exchanges": sorted(admin_settings.get_disabled_wallet_exchanges()),
        "disabled_chains": sorted(admin_settings.get_disabled_chains()),
        "disabled_perpdexes": sorted(admin_settings.get_disabled_perpdexes()),
    }


@router.patch("/portfolio-config")
def portfolio_config_patch(
    body: PortfolioConfigIn,
    user: User = Depends(get_admin_user),
):
    inv = _portfolio_inventory()
    if body.disabled_wallet_exchanges is not None:
        known = set(inv["available_wallet_exchanges"])
        cleaned = sorted({str(s).strip().lower() for s in body.disabled_wallet_exchanges if str(s).strip().lower() in known})
        admin_settings.set_value(admin_settings.KEY_DISABLED_WALLET_EXCHANGES, cleaned, user_id=user.id)
    if body.disabled_chains is not None:
        known = set(inv["available_chains"])
        cleaned = sorted({str(s).strip().lower() for s in body.disabled_chains if str(s).strip().lower() in known})
        admin_settings.set_value(admin_settings.KEY_DISABLED_CHAINS, cleaned, user_id=user.id)
    if body.disabled_perpdexes is not None:
        known = set(inv["available_perpdexes"])
        cleaned = sorted({str(s).strip().lower() for s in body.disabled_perpdexes if str(s).strip().lower() in known})
        admin_settings.set_value(admin_settings.KEY_DISABLED_PERPDEXES, cleaned, user_id=user.id)
    return portfolio_config_get(user)


# ═══ Funding WS health ════════════════════════════════════════════════════════

@router.get("/price-anomalies")
def admin_price_anomalies(_: User = Depends(get_admin_user)):
    """Running count of price anomalies per exchange since process start.
    Useful to spot exchanges silently feeding bad prices (KuCoin, etc.)."""
    from backend.services.arbitrage_service import price_anomaly_counters
    counters = price_anomaly_counters()
    total = sum(counters.values())
    return {"total": total, "by_exchange": counters}


@router.get("/funding-ws-health")
def funding_ws_health(_: User = Depends(get_admin_user)):
    """Per-exchange WS funding stream health — used to tell at a glance
    which adapters are up and how fresh their data is."""
    from backend.services.funding_ws import ws_health, ADAPTERS
    health = ws_health()
    # Ensure every supported adapter shows up even if manager hasn't
    # started on this worker yet.
    for ex in ADAPTERS:
        health.setdefault(ex, {"connected": False, "symbols": 0, "last_age_s": None, "healthy": False})
    return {"adapters": health}


# ═══ Logs ═════════════════════════════════════════════════════════════════════

@router.get("/logs")
def admin_logs(
    role: str = Query("fetcher", pattern="^(web|fetcher|monolith)$"),
    channel: str = Query("errors", pattern="^(errors|full)$"),
    lines: int = Query(200, ge=1, le=5000),
    _: User = Depends(get_admin_user),
):
    """Tail the most recent lines of a log file written by setup_logging().

    Works cross-role only when the admin's web container mounts the same
    `avalant_logs` volume as the fetcher — which the docker-compose does.
    """
    from pathlib import Path
    from backend.logging_config import get_log_dir

    # get_log_dir() returns the dir of the CURRENT process's role, not
    # necessarily the one the admin wants. Walk one level up to reach the
    # shared log root and pick the requested role subdir.
    own = get_log_dir()
    if own is None:
        # File logging disabled on this process — point at the default root.
        base = Path(os.environ.get("AVALANT_LOG_DIR", "/var/log/avalant"))
    else:
        base = own.parent

    target = base / role / f"{channel}.log"
    if not target.exists():
        return {"role": role, "channel": channel, "path": str(target),
                "lines": [], "note": "log file not found yet"}

    try:
        # Efficient tail: read last ~1MB, split, keep last N lines.
        size = target.stat().st_size
        read_bytes = min(size, 2 * 1024 * 1024)
        with target.open("rb") as f:
            f.seek(max(0, size - read_bytes))
            tail = f.read().decode("utf-8", errors="replace")
        content = tail.splitlines()[-lines:]
    except Exception as exc:
        raise HTTPException(500, f"log read failed: {exc}") from exc

    return {"role": role, "channel": channel, "path": str(target),
            "lines": content, "count": len(content)}


@router.get("/data-plane-health")
def admin_data_plane_health(_: User = Depends(get_admin_user)):
    """Observability endpoint for the fetcher sidecar.

    Every data-plane output file has an implicit heartbeat: if the
    owner is alive and healthy, the mtime is recent. A stale file
    means the fetcher hung or died while still holding the file lock
    — nothing else can take over until it's killed. This endpoint
    surfaces ages so ops can decide to restart.
    """
    from pathlib import Path
    cache_dir = Path("/tmp/avalant_cache")
    # (filename, "what it is", expected refresh cadence in seconds,
    #  age threshold at which we report unhealthy)
    channels = [
        ("funding_ws.json",      "funding WS dump",     2.0,  30.0),
        ("funding.json",         "merged funding data", 3.0,  30.0),
        ("arbitrage.json",       "arbitrage opps",      4.0,  60.0),
        ("books.json",           "orderbook prewarm",   5.0,  60.0),
        ("price_anomalies.json", "price anomaly tally", 60.0, 600.0),
    ]
    now = time.time()
    channels_out = []
    overall_healthy = True
    for name, label, cadence, unhealthy_at in channels:
        path = cache_dir / name
        if not path.exists():
            channels_out.append({
                "file": name, "label": label, "age_s": None,
                "expected_cadence_s": cadence, "healthy": False,
                "note": "file missing — fetcher never wrote it",
            })
            overall_healthy = False
            continue
        age = now - path.stat().st_mtime
        healthy = age <= unhealthy_at
        if not healthy:
            overall_healthy = False
        channels_out.append({
            "file": name, "label": label, "age_s": round(age, 1),
            "expected_cadence_s": cadence, "healthy": healthy,
            "unhealthy_after_s": unhealthy_at,
        })
    return {"healthy": overall_healthy, "channels": channels_out}


@router.get("/logs/roles")
def admin_logs_roles(_: User = Depends(get_admin_user)):
    """List which roles have logs written so the UI can show tabs only for
    what actually exists on disk."""
    from pathlib import Path
    from backend.logging_config import get_log_dir
    own = get_log_dir()
    base = own.parent if own else Path(os.environ.get("AVALANT_LOG_DIR", "/var/log/avalant"))
    roles = []
    if base.exists():
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            errors = child / "errors.log"
            full = child / "full.log"
            roles.append({
                "role": child.name,
                "errors_bytes": errors.stat().st_size if errors.exists() else 0,
                "full_bytes":   full.stat().st_size if full.exists() else 0,
            })
    return {"root": str(base), "roles": roles}


# ═════════════════════════════════════════════════════════════════════════════
#  Pricing / promos / popups admin CRUD
# ═════════════════════════════════════════════════════════════════════════════
from backend.services import (
    plan_service as _plan_service,
    promo_service as _promo_service,
    popup_service as _popup_service,
)
from backend.db.models import Plan as _Plan, PromoCode as _PromoCode, Popup as _Popup


# ── Plans ────────────────────────────────────────────────────────────────────
@router.get("/plans")
def admin_list_plans(
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    rows = _plan_service.list_plans(db, only_active=False)
    return {"plans": [_plan_service.serialize_plan(p) for p in rows]}


@router.post("/plans")
def admin_create_plan(
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    slug = (body.get("slug") or "").strip().lower()
    if not slug or not slug.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=422, detail="slug is required (alnum / -_ only)")
    if _plan_service.get_plan_by_slug(db, slug):
        raise HTTPException(status_code=409, detail="slug already exists")
    plan = _plan_service.create_plan(db, slug, body)
    audit_log.record(db, request=request, actor=current_admin,
                     action="plan.create", target_type="plan", target_id=plan.id,
                     delta={"slug": slug, "fields": {k: body.get(k) for k in body if k != "features"}})
    return _plan_service.serialize_plan(plan)


@router.patch("/plans/{plan_id}")
def admin_update_plan(
    plan_id: int,
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    plan = _plan_service.get_plan(db, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    _plan_service.update_plan(db, plan, body)
    audit_log.record(db, request=request, actor=current_admin,
                     action="plan.update", target_type="plan", target_id=plan.id,
                     delta={k: body.get(k) for k in body if k != "features"})
    return _plan_service.serialize_plan(plan)


@router.delete("/plans/{plan_id}")
def admin_delete_plan(
    plan_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    plan = _plan_service.get_plan(db, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    try:
        _plan_service.delete_plan(db, plan)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_log.record(db, request=request, actor=current_admin,
                     action="plan.delete", target_type="plan", target_id=plan.id,
                     delta={"slug": plan.slug, "name": plan.name})
    return {"ok": True}


# ── Promo codes ──────────────────────────────────────────────────────────────
@router.get("/promos")
def admin_list_promos(
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    rows = _promo_service.list_codes(db, only_active=False)
    return {"promos": [_promo_service.serialize_code(p) for p in rows]}


@router.get("/promos/stats")
def admin_promo_stats(
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    return {"stats": _promo_service.stats(db)}


@router.post("/promos")
def admin_create_promo(
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    code = (body.get("code") or "").strip()
    try:
        promo = _promo_service.create_code(db, code, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    audit_log.record(db, request=request, actor=current_admin,
                     action="promo.create", target_type="promo", target_id=promo.id,
                     delta={"code": promo.code, "discount_pct": float(promo.discount_pct)})
    return _promo_service.serialize_code(promo)


@router.patch("/promos/{promo_id}")
def admin_update_promo(
    promo_id: int,
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    promo = db.query(_PromoCode).filter(_PromoCode.id == promo_id).first()
    if not promo:
        raise HTTPException(status_code=404, detail="promo not found")
    _promo_service.update_code(db, promo, body)
    audit_log.record(db, request=request, actor=current_admin,
                     action="promo.update", target_type="promo", target_id=promo.id,
                     delta=body)
    return _promo_service.serialize_code(promo)


@router.delete("/promos/{promo_id}")
def admin_delete_promo(
    promo_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    promo = db.query(_PromoCode).filter(_PromoCode.id == promo_id).first()
    if not promo:
        raise HTTPException(status_code=404, detail="promo not found")
    code_str = promo.code
    _promo_service.delete_code(db, promo)
    audit_log.record(db, request=request, actor=current_admin,
                     action="promo.delete", target_type="promo", target_id=promo_id,
                     delta={"code": code_str})
    return {"ok": True}


# ── Popups ───────────────────────────────────────────────────────────────────
@router.get("/popups")
def admin_list_popups(
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    rows = _popup_service.list_popups(db, only_active=False)
    return {"popups": [_popup_service.serialize_popup(p) for p in rows]}


@router.post("/popups")
def admin_create_popup(
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    try:
        popup = _popup_service.create_popup(db, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    audit_log.record(db, request=request, actor=current_admin,
                     action="popup.create", target_type="popup", target_id=popup.id,
                     delta={"title": popup.title, "target": popup.target_type})
    return _popup_service.serialize_popup(popup)


@router.patch("/popups/{popup_id}")
def admin_update_popup(
    popup_id: int,
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    popup = db.query(_Popup).filter(_Popup.id == popup_id).first()
    if not popup:
        raise HTTPException(status_code=404, detail="popup not found")
    _popup_service.update_popup(db, popup, body)
    audit_log.record(db, request=request, actor=current_admin,
                     action="popup.update", target_type="popup", target_id=popup.id, delta=body)
    return _popup_service.serialize_popup(popup)


@router.delete("/popups/{popup_id}")
def admin_delete_popup(
    popup_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    popup = db.query(_Popup).filter(_Popup.id == popup_id).first()
    if not popup:
        raise HTTPException(status_code=404, detail="popup not found")
    title = popup.title
    _popup_service.delete_popup(db, popup)
    audit_log.record(db, request=request, actor=current_admin,
                     action="popup.delete", target_type="popup", target_id=popup_id,
                     delta={"title": title})
    return {"ok": True}


@router.get("/users/search")
def admin_users_search(
    q: str = Query("", max_length=64),
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    """Lightweight typeahead for the popup target picker.

    Length cap on `q` (64 chars) prevents pathologically long ILIKE
    patterns from exhausting DB time. The actual SQL is parameter-bound
    via SQLAlchemy's expression API — no string interpolation reaches
    the wire — so user-supplied `%` / `_` / quotes are interpreted as
    LIKE wildcards / literals, never as SQL syntax."""
    qs = (q or "").strip().lower()
    if not qs:
        return {"users": []}
    # Escape LIKE wildcards so a user typing "%" doesn't match every
    # username — keeps the typeahead semantically sane and prevents
    # accidental "match-everything" queries.
    safe = qs.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{safe}%"
    rows = (
        db.query(User)
        .filter(
            User.username.ilike(pattern, escape="\\")
            | User.email.ilike(pattern, escape="\\")
        )
        .order_by(User.username.asc())
        .limit(20)
        .all()
    )
    return {"users": [{"id": u.id, "username": u.username, "email": u.email} for u in rows]}


# ── Billing periods ───────────────────────────────────────────────────────
from backend.services import billing_period_service as _bp_service
from backend.db.models import BillingPeriod as _BillingPeriod


@router.get("/billing-periods")
def admin_list_billing_periods(
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    rows = _bp_service.list_periods(db, only_active=False)
    return {"billing_periods": [_bp_service.serialize(p) for p in rows]}


@router.post("/billing-periods")
def admin_create_billing_period(
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    slug = (body.get("slug") or "").strip().lower()
    try:
        period = _bp_service.create(db, slug, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    audit_log.record(db, request=request, actor=current_admin,
                     action="billing_period.create", target_type="billing_period",
                     target_id=period.id, delta={"slug": slug, "months": period.months})
    return _bp_service.serialize(period)


@router.patch("/billing-periods/{period_id}")
def admin_update_billing_period(
    period_id: int,
    body: dict,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    period = _bp_service.get_period(db, period_id)
    if not period:
        raise HTTPException(status_code=404, detail="period not found")
    _bp_service.update(db, period, body)
    audit_log.record(db, request=request, actor=current_admin,
                     action="billing_period.update", target_type="billing_period",
                     target_id=period.id, delta=body)
    return _bp_service.serialize(period)


@router.delete("/billing-periods/{period_id}")
def admin_delete_billing_period(
    period_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    period = _bp_service.get_period(db, period_id)
    if not period:
        raise HTTPException(status_code=404, detail="period not found")
    slug = period.slug
    _bp_service.delete(db, period)
    audit_log.record(db, request=request, actor=current_admin,
                     action="billing_period.delete", target_type="billing_period",
                     target_id=period_id, delta={"slug": slug})
    return {"ok": True}


# ── Audit log read-only endpoint for admins ───────────────────────────────
from backend.db.models import AuditLogEntry as _AuditLogEntry


@router.get("/audit-log")
def admin_audit_log(
    limit: int = Query(100, ge=1, le=500),
    action: str | None = None,
    actor_user_id: int | None = None,
    target_type: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
):
    q = db.query(_AuditLogEntry).order_by(_AuditLogEntry.created_at.desc())
    if action:
        q = q.filter(_AuditLogEntry.action == action)
    if actor_user_id:
        q = q.filter(_AuditLogEntry.actor_user_id == actor_user_id)
    if target_type:
        q = q.filter(_AuditLogEntry.target_type == target_type)
    rows = q.limit(limit).all()
    return {"entries": [audit_log.serialize(e) for e in rows]}


# ── Admin broadcast: send a TG message via the auth bot ───────────────────────
class _BroadcastBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    target: str = Field(default="all", description="'all' | 'user'")
    target_user_id: int | None = None
    parse_mode: str = Field(default="HTML")


@router.post("/broadcast")
async def admin_broadcast(
    body: _BroadcastBody,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_admin_user),
):
    """Send a Telegram message via the auth bot to every linked user (target='all')
    or a single user (target='user', target_user_id required). HTML parse mode by
    default — admin can paste <b>, <i>, <a href>, etc. Replies use the auth bot
    so the user-facing alerts firehose stays separate from system announcements.
    Returns {sent, failed} counts; never raises on per-recipient send failures
    so a single banned chat doesn't abort the whole batch."""
    from settings import settings as _settings
    import httpx, asyncio

    token = (_settings.TG_AUTH_BOT_TOKEN or _settings.TG_BOT_TOKEN or "").strip()
    if not token:
        raise HTTPException(status_code=503, detail="No TG bot token configured on server")

    parse_mode = (body.parse_mode or "").strip()
    if parse_mode not in ("HTML", "MarkdownV2", ""):
        raise HTTPException(status_code=400, detail="parse_mode must be HTML or MarkdownV2 (or empty)")

    target = (body.target or "all").lower()
    if target == "user":
        if not body.target_user_id:
            raise HTTPException(status_code=422, detail="target_user_id required when target='user'")
        rows = (
            db.query(User.id, User.tg_chat_id, User.username)
            .filter(User.id == body.target_user_id, User.tg_chat_id.isnot(None))
            .all()
        )
    elif target == "all":
        rows = (
            db.query(User.id, User.tg_chat_id, User.username)
            .filter(User.tg_chat_id.isnot(None), User.is_blocked.is_(False))
            .all()
        )
    else:
        raise HTTPException(status_code=400, detail="target must be 'all' or 'user'")

    if not rows:
        return {"sent": 0, "failed": 0, "skipped": "no eligible recipients"}

    async def _one(chat_id: int) -> bool:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id, "text": body.text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(url, json=payload)
                return bool(r.json().get("ok"))
        except Exception:
            return False

    # Cap concurrency so we don't trip TG flood-wait at scale.
    sem = asyncio.Semaphore(20)
    async def _bounded(chat_id: int) -> bool:
        async with sem:
            return await _one(chat_id)

    results = await asyncio.gather(*(_bounded(int(r.tg_chat_id)) for r in rows),
                                   return_exceptions=True)
    sent = sum(1 for r in results if r is True)
    failed = len(rows) - sent

    audit_log.record(
        db, request=request, actor=current_admin,
        action="admin.broadcast", target_type="users", target_id=None,
        delta={"target": target, "target_user_id": body.target_user_id,
               "recipients": len(rows), "sent": sent, "failed": failed,
               "preview": body.text[:120]},
    )
    return {"sent": sent, "failed": failed, "recipients": len(rows)}

@router.get("/freshness-stats")
def freshness_statistics(_: User = Depends(get_admin_user)):
    """Rolling 5-minute average freshness per exchange + overall.

    Sourced from backend.services.freshness_stats, which records every
    sample produced by `/api/screener/exchange-health` (called from the
    UI Exchange-status strip every 3 s). Means the dashboard reflects
    what real users are actually seeing without the admin endpoint
    needing its own poll loop."""
    from backend.services.freshness_stats import stats as _stats
    return _stats()
