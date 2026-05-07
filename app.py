import logging
import os as _os_boot
import warnings
from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from backend.logging_config import setup_logging, install_asyncio_hook
from settings import settings

_role = (_os_boot.environ.get("AVALANT_ROLE", "").lower() or "monolith")
setup_logging(_role, level=settings.LOG_LEVEL)
logger = logging.getLogger("avalant")

_INSECURE_DEFAULTS = {
    "change-me-in-production-use-a-long-random-string",
}


def _check_security():
    """Refuse to boot when dangerous default secrets are in use.

    The previous behaviour was a warning that boots anyway — easy to
    miss in CI logs and easy to ship with. Production-readiness audit
    upgrades it to a hard fail. The local-dev `wallet_monitor.db` path
    on SQLite is the one allowed exception (no real prod data at risk).
    """
    issues = []
    if settings.SECRET_KEY in _INSECURE_DEFAULTS:
        issues.append("SECRET_KEY is using the default insecure value")
    if settings.ENCRYPTION_KEY in _INSECURE_DEFAULTS:
        issues.append("ENCRYPTION_KEY is using the default insecure value")
    if not issues:
        return
    is_local_sqlite = "sqlite:" in (settings.DATABASE_URL or "").lower()
    msg = (
        "\n" + "=" * 60 +
        "\n  ⚠  SECURITY:\n" +
        "\n".join(f"  • {i}" for i in issues) +
        "\n  Set these in your .env file before deploying!" +
        "\n" + "=" * 60
    )
    logger.error(msg)
    if not is_local_sqlite:
        raise RuntimeError(
            "Refusing to start with default secrets on a non-SQLite database. "
            "Override SECRET_KEY and ENCRYPTION_KEY in .env."
        )


def run_migrations():
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True, text=True, timeout=60,
    )
    if result.stdout:
        logger.info("Alembic: %s", result.stdout.strip())
    if result.returncode != 0:
        logger.error("Migration failed: %s", result.stderr.strip())
        raise RuntimeError("Migration failed")


def _ensure_system_tags() -> None:
    from backend.db.base import SessionLocal
    from backend.db.models import Tag
    db = SessionLocal()
    try:
        if not db.query(Tag).filter(Tag.name == "Owner", Tag.user_id == None).first():
            db.add(Tag(name="Owner", color="#1AFFAB", user_id=None))
            db.commit()
            logger.info("Created system tag: Owner")
    except Exception:
        db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    install_asyncio_hook()
    _check_security()
    logger.info("Starting Avalant")
    # Only one instance runs alembic — set AVALANT_RUN_MIGRATIONS=false
    # on the secondary `app2` so two replicas don't race the
    # alembic_version row (we've seen "0 found" errors during rolling
    # deploys when both started at once).
    if (_os_boot.environ.get("AVALANT_RUN_MIGRATIONS", "true").strip().lower() != "false"):
        run_migrations()
        _ensure_system_tags()
    else:
        logger.info("Migrations skipped (AVALANT_RUN_MIGRATIONS=false)")
    logger.info("Migrations applied — server ready")

    # Role decides what runs here. Default (empty/monolith) = everything, for
    # backwards-compat with single-container deploys. When running sidecar'd,
    # docker-compose sets AVALANT_ROLE=web on the uvicorn container and
    # AVALANT_ROLE=fetcher on the data-plane sidecar (python -m fetcher).
    role = _os_boot.environ.get("AVALANT_ROLE", "").lower() or "monolith"
    is_web = role == "web"
    logger.info("Avalant role: %s", role)

    # ── Always — cheap, lives in the HTTP process ─────────────────────
    from backend.api.v1.screener import start_broadcast_loop, stop_broadcast_loop
    from backend.api.v1.screener import start_book_broadcast_loop, stop_book_broadcast_loop
    start_broadcast_loop()
    start_book_broadcast_loop()

    # Price loop runs in EVERY process (web + fetcher + monolith).
    # _prices is in-memory per-process, and balance_service.get_usd_value
    # reads it on the web container when serving /api/portfolio/balance.
    # Previously gated behind `if not is_web`, which left the web worker
    # with an empty price cache → all portfolio tokens rendered with no
    # USD value. The loop itself is cheap: 1 CMC + 1 Gate call / 30min.
    _stop_fns = []
    from backend.services.price_service import start_price_loop, stop_price_loop
    start_price_loop()
    _stop_fns.append(stop_price_loop)
    logger.info("Price refresh loop started (role=%s)", role)

    # ── Always on web + monolith (not fetcher sidecar) ────────────────
    # These services need DB + TG — they're safe to run on both replicas
    # (TG bot uses Redis leader election; alert cooldown is DB-persisted).
    from backend.services.alert_service import start_alert_service, stop_alert_service
    start_alert_service()
    _stop_fns.append(stop_alert_service)

    from backend.services.tg_bot_service import start_tg_bot, stop_tg_bot
    start_tg_bot()
    _stop_fns.append(stop_tg_bot)

    from backend.services.expiry_notifier_service import (
        start_expiry_notifier, stop_expiry_notifier,
    )
    start_expiry_notifier()
    _stop_fns.append(stop_expiry_notifier)

    # Trigger-order daemon: 1s polling loop with atomic claim-on-fire SQL.
    # Safe to run on both replicas — atomic UPDATE ensures exactly-once
    # per trigger across the cluster.
    from backend.services import trigger_order_service
    trigger_order_service.start()

    # Watchlist → orderbook-subscribe bridge. Dumps distinct
    # (sym, long_ex, short_ex) across all users every 30s so the
    # Go symbol-manager keeps watched pairs subscribed even when
    # they fall out of the top-N tracked set.
    from backend.services.watchlist_subscribe_dump import (
        start_watchlist_dump, stop_watchlist_dump,
    )
    start_watchlist_dump()
    _stop_fns.append(stop_watchlist_dump)

    # ── Monolith-only (web process handles everything when no sidecar) ─
    if not is_web:
        from backend.api.v1.screener import start_refresh_loop, stop_refresh_loop
        start_refresh_loop()
        _stop_fns.append(stop_refresh_loop)

        from backend.services.spot_arbitrage_service import (
            start_spot_refresh_loop, stop_spot_refresh_loop,
        )
        start_spot_refresh_loop()
        _stop_fns.append(stop_spot_refresh_loop)

        from backend.services.dex_arbitrage_service import (
            start_dex_refresh_loop, stop_dex_refresh_loop,
        )
        start_dex_refresh_loop()
        _stop_fns.append(stop_dex_refresh_loop)

        from backend.services.orderbook_cache import start_prewarm, stop_prewarm
        start_prewarm()
        _stop_fns.append(stop_prewarm)

        from backend.services.funding_ws import (
            start_funding_ws_manager, stop_funding_ws_manager,
        )
        start_funding_ws_manager()
        _stop_fns.append(stop_funding_ws_manager)

        import asyncio, fcntl
        _alpha_tasks = []
        _alpha_lock_fd = None
        try:
            _alpha_lock_fd = open("/tmp/avalant_alpha.lock", "w")
            fcntl.flock(_alpha_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            from backend.services.health_service import health_loop
            from backend.services.replay_service import snapshot_loop
            from backend.services.anomaly_service import anomaly_loop
            _alpha_tasks = [
                asyncio.create_task(health_loop(interval_s=60)),
                asyncio.create_task(snapshot_loop(interval_s=60)),
                asyncio.create_task(anomaly_loop(interval_s=120)),
            ]
            logger.info("Alpha loops started (health, snapshot, anomaly)")
        except (IOError, OSError):
            logger.info("Alpha loops: another worker holds lock — skipping")
            _alpha_tasks = []
    else:
        _alpha_tasks = []
        logger.info("Web mode — data plane runs in the fetcher sidecar")

    try:
        yield
    finally:
        for t in _alpha_tasks:
            t.cancel()
        for fn in _stop_fns:
            try:
                fn()
            except Exception:
                logger.exception("stop_fn %s failed", getattr(fn, "__name__", fn))
        try:
            stop_broadcast_loop()
        except Exception:
            logger.exception("stop_broadcast_loop failed")
        try:
            stop_book_broadcast_loop()
        except Exception:
            logger.exception("stop_book_broadcast_loop failed")
        logger.info("Avalant shutting down")


app = FastAPI(
    title="Avalant",
    version="1.0.0",
    lifespan=lifespan,
    # Hide internal details from public error responses
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
_origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,          # empty = same-origin only (no CORS headers)
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ── Maintenance mode ──────────────────────────────────────────────────────────
# Enable by creating the flag file (no restart needed):
#   touch /tmp/avalant_maintenance
# Disable:
#   rm /tmp/avalant_maintenance
# When active: every HTML request returns frontend/maintenance.html (503).
# Static assets still serve (so the maintenance page renders), and /api/health
# still works (so uptime monitors don't flap).
import os as _os
_MAINT_FLAG = "/tmp/avalant_maintenance"
_MAINT_BYPASS_PREFIXES = ("/api/health", "/avalant_favicon", "/favicon.ico",
                          "/avalant-logo", "/og-image",
                          "/navbar.css", "/navbar.js", "/auth.js", "/theme.js",
                          "/toast.js")

def _maintenance_on() -> bool:
    if _os.path.exists(_MAINT_FLAG):
        return True
    try:
        from backend.services import admin_settings
        return admin_settings.is_maintenance()
    except Exception:
        return False


# Paths blocked by per-section maintenance flags. /api/admin/* and the
# auth/health endpoints are intentionally NEVER in either set so admins can
# always toggle the flags back off and uptime monitors keep working.
_SCREENER_PATHS = ("/screener", "/arb", "/watchlist")
_SCREENER_API_PREFIXES = ("/api/screener/",)
# Portfolio scope covers everything that touches a user's wallet/account
# data — but DOES NOT include /pricing or /checkout so a user with a near-
# expired plan can still renew while we're working on the portfolio side.
_PORTFOLIO_PATHS = ("/portfolio", "/app", "/archive", "/profile", "/avashare")
_PORTFOLIO_API_PREFIXES = (
    "/api/wallets", "/api/portfolio", "/api/alerts", "/api/trade",
    "/api/popups",  # popups are tied to the logged-in experience
)


def _section_maintenance_html(section: str, title: str, body: str,
                              ends_at: str | None = None, tz: str | None = None,
                              scope: str = "screener") -> str:
    """Standalone inline page — no static-asset deps, so it renders even
    during full-site maintenance. ETA + countdown render only when ends_at
    is a future ISO datetime; client polls /api/maintenance/status every
    15 s and reloads when scope flips back on."""
    eta_block = ""
    if ends_at:
        eta_block = f"""
  <div class="eta">
    <div class="eta-lbl">Expected to end</div>
    <div class="eta-time" id="eta-abs">—</div>
    <div class="eta-count" id="eta-cd">—</div>
  </div>"""
    return f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{title} · avalant_</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel=stylesheet>
<style>
html,body{{margin:0;padding:0;background:#0E0E11;color:#E6E8E3;font-family:Inter,system-ui,sans-serif;height:100%;-webkit-font-smoothing:antialiased;}}
.wrap{{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;}}
.card{{max-width:460px;width:100%;background:#131217;border:1px solid #22222A;border-radius:14px;padding:32px 28px;text-align:center;box-shadow:0 24px 60px rgba(0,0,0,0.45);}}
.icon{{width:52px;height:52px;margin:0 auto 18px;border-radius:13px;display:flex;align-items:center;justify-content:center;background:rgba(229,192,123,0.12);border:1px solid rgba(229,192,123,0.4);color:#E5C07B;}}
.section{{display:inline-block;font-size:10px;font-weight:700;letter-spacing:0.14em;padding:3px 9px;border-radius:5px;background:rgba(26,255,171,0.08);color:#1AFFAB;border:1px solid rgba(26,255,171,0.3);text-transform:uppercase;margin-bottom:10px;}}
h1{{margin:0 0 10px;font-size:21px;letter-spacing:-0.01em;font-weight:700;}}
p{{margin:0 0 16px;color:#9B9FAB;font-size:13.5px;line-height:1.55;}}
.eta{{margin:18px 0 22px;padding:14px;background:rgba(26,255,171,0.04);border:1px solid rgba(26,255,171,0.18);border-radius:10px;}}
.eta-lbl{{font-size:10px;font-weight:700;letter-spacing:0.12em;color:#1AFFAB;text-transform:uppercase;margin-bottom:6px;}}
.eta-time{{font-family:'JetBrains Mono',monospace;font-size:15px;color:#E6E8E3;font-weight:600;letter-spacing:-0.01em;}}
.eta-count{{font-family:'JetBrains Mono',monospace;font-size:12px;color:#9B9FAB;margin-top:6px;}}
.cta{{display:inline-block;padding:10px 22px;border-radius:9px;background:#1AFFAB;color:#0a0a0f;font-weight:700;font-size:13px;text-decoration:none;letter-spacing:-0.005em;transition:transform .15s,box-shadow .15s;}}
.cta:hover{{transform:translateY(-1px);box-shadow:0 6px 18px rgba(26,255,171,0.25);}}
.brand{{margin-top:26px;font-weight:800;font-size:14px;letter-spacing:-0.02em;color:#676B7E;}}
.brand span{{color:#1AFFAB;animation:blink 1s infinite;}}
@keyframes blink{{50%{{opacity:0;}}}}
</style></head><body>
<div class=wrap><div class=card>
  <div class=icon><svg width="24" height="24" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="7" width="10" height="7" rx="1.5"/><path d="M5 7V5a3 3 0 016 0v2"/></svg></div>
  <div class=section>{section}</div>
  <h1>{title}</h1>
  <p>{body}</p>{eta_block}
  <a class=cta href="/">Back to home</a>
  <div class=brand>avalant<span>_</span></div>
</div></div>
<script>
(function(){{
  var endsAt = {f'"{ends_at}"' if ends_at else 'null'};
  var tzName = {f'"{tz or "Europe/Warsaw"}"'};
  var scope  = "{scope}";

  function fmtAbs(iso){{
    try{{
      var d = new Date(iso);
      // Format in target TZ. Falls back gracefully if browser doesn't know
      // the IANA name (very old builds), in which case it just shows local.
      var opts = {{ timeZone: tzName, hour: '2-digit', minute: '2-digit',
                    day: '2-digit', month: 'short', hour12: false }};
      return d.toLocaleString('en-GB', opts) + " (" + tzName + ")";
    }}catch(e){{ return new Date(iso).toLocaleString(); }}
  }}
  function fmtCd(iso){{
    var diff = (new Date(iso).getTime() - Date.now()) / 1000;
    if (diff <= 0) return "Wrapping up…";
    var h = Math.floor(diff / 3600);
    var m = Math.floor((diff % 3600) / 60);
    var s = Math.floor(diff % 60);
    return (h ? h + "h " : "") + (m < 10 ? "0" + m : m) + "m " + (s < 10 ? "0" + s : s) + "s remaining";
  }}
  function tick(){{
    if (!endsAt) return;
    var abs = document.getElementById('eta-abs');
    var cd  = document.getElementById('eta-cd');
    if (abs) abs.textContent = fmtAbs(endsAt);
    if (cd)  cd.textContent  = fmtCd(endsAt);
  }}
  tick();
  if (endsAt) setInterval(tick, 1000);

  async function poll(){{
    try{{
      var r = await fetch('/api/maintenance/status', {{cache:'no-store'}});
      if (!r.ok) return;
      var s = await r.json();
      var stillBlocked =
        (scope === 'site'      && s.maintenance) ||
        (scope === 'screener'  && (s.maintenance || s.screener_disabled)) ||
        (scope === 'portfolio' && (s.maintenance || s.portfolio_disabled));
      if (!stillBlocked) location.reload();
    }}catch(_){{}}
  }}
  setInterval(poll, 15000);
}})();
</script>
</body></html>"""


def _is_screener_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") or path.startswith(p + "?") for p in _SCREENER_PATHS) \
        or path.startswith(_SCREENER_API_PREFIXES)


def _is_portfolio_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") or path.startswith(p + "?") for p in _PORTFOLIO_PATHS) \
        or path.startswith(_PORTFOLIO_API_PREFIXES)


@app.middleware("http")
async def maintenance_gate(request: Request, call_next) -> Response:
    from fastapi.responses import FileResponse as _FR
    from starlette.responses import HTMLResponse, JSONResponse
    path = request.url.path

    if _maintenance_on():
        # Allow monitor hits + the new public status endpoint (so the
        # maintenance page can poll-and-reload) + admin API + static assets
        # needed by the rendered HTML.
        allow = path in ("/maintenance", "/maintenance.html") or \
                path == "/api/maintenance/status" or \
                path.startswith(_MAINT_BYPASS_PREFIXES) or \
                path.startswith("/api/admin/")
        if not allow:
            try:
                from backend.services import admin_settings
                ends_at = admin_settings.get_maintenance_ends_at()
                tz = admin_settings.get_maintenance_tz()
            except Exception:
                ends_at, tz = None, "Europe/Warsaw"
            return HTMLResponse(
                _section_maintenance_html(
                    "Full-site maintenance",
                    "We're working on the site",
                    "All sections are paused while we ship updates. The page will reload itself when we're back.",
                    ends_at=ends_at, tz=tz, scope="site",
                ),
                status_code=503,
                headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Retry-After": "300"},
            )

    # Per-section soft maintenance — /api/admin/* always stays reachable so
    # the toggle can be flipped back off.
    if not path.startswith("/api/admin/"):
        try:
            from backend.services import admin_settings
            tz = admin_settings.get_maintenance_tz()
            if admin_settings.is_screener_disabled() and _is_screener_path(path):
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Screener temporarily disabled"}, status_code=503)
                return HTMLResponse(
                    _section_maintenance_html(
                        "Screener",
                        "Screener temporarily unavailable",
                        "We're doing scheduled maintenance on the screener and arbitrage pages. Funding rates and pair pages will be back shortly.",
                        ends_at=admin_settings.get_screener_disabled_ends_at(),
                        tz=tz, scope="screener",
                    ),
                    status_code=503,
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Retry-After": "300"},
                )
            if admin_settings.is_portfolio_disabled() and _is_portfolio_path(path):
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Portfolio temporarily disabled"}, status_code=503)
                return HTMLResponse(
                    _section_maintenance_html(
                        "Portfolio",
                        "Portfolio temporarily unavailable",
                        "Your portfolio, balance fetches, and alerts are paused for maintenance. The screener stays available.",
                        ends_at=admin_settings.get_portfolio_disabled_ends_at(),
                        tz=tz, scope="portfolio",
                    ),
                    status_code=503,
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Retry-After": "300"},
                )
        except Exception:
            pass

    return await call_next(request)


# Content-Security-Policy. The frontend ships inline <script>/<style> blocks
# everywhere, so 'unsafe-inline' has to stay until we move to a build step
# with hashes/nonces. Even with that caveat, locking down origins narrows
# the XSS blast radius — only avalant origins, the Google Fonts pair, and
# unpkg (Lightweight Charts CDN) can serve resources to a logged-in user.
_CSP = "; ".join([
    "default-src 'self'",
    # telegram.org hosts the official Login Widget script (telegram-widget.js).
    "script-src 'self' 'unsafe-inline' https://unpkg.com https://telegram.org",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' data: https://fonts.gstatic.com",
    # img-src must allow exchange logos/avatars + base64 inline + telegram CDN.
    "img-src 'self' data: blob: https:",
    # Same-origin XHR + WSS (covers /api/* and /ws/screener/ws/*) plus
    # DexScreener which the spot/dex chart hits client-side. All exchange
    # APIs are proxied through our backend so we don't need to allow them
    # here. Narrows the XSS exfil surface from "any HTTPS host" to two.
    "connect-src 'self' https://api.dexscreener.com",
    # The Login Widget renders an iframe pointing at oauth.telegram.org.
    # DexScreener iframe is the chart on /arb?type=dex pages.
    "frame-src 'self' https://oauth.telegram.org https://telegram.org https://dexscreener.com",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "upgrade-insecure-requests",
])


# Per-extension cache windows. HTML stays no-cache so ops can hot-fix copy
# without telling users to refresh. Everything else (JS / CSS / fonts /
# icons) gets a 5-minute browser cache + 1-hour CDN/proxy cache, so a
# returning user pulls a single round of HTML and reuses every static
# asset across reloads. Tighten further once we ship hash-named bundles.
_STATIC_CACHE_BY_SUFFIX = {
    ".js":   "public, max-age=300, s-maxage=3600",
    ".css":  "public, max-age=300, s-maxage=3600",
    ".svg":  "public, max-age=86400, s-maxage=86400, immutable",
    ".png":  "public, max-age=86400, s-maxage=86400",
    ".jpg":  "public, max-age=86400, s-maxage=86400",
    ".jpeg": "public, max-age=86400, s-maxage=86400",
    ".webp": "public, max-age=86400, s-maxage=86400",
    ".ico":  "public, max-age=604800, s-maxage=604800, immutable",
    ".woff": "public, max-age=2592000, immutable",
    ".woff2":"public, max-age=2592000, immutable",
    ".ttf":  "public, max-age=2592000, immutable",
}


def _static_cache_for(path: str) -> str | None:
    p = path.lower()
    for suf, cc in _STATIC_CACHE_BY_SUFFIX.items():
        if p.endswith(suf):
            return cc
    return None


# ── Security headers ──────────────────────────────────────────────────────────
@app.middleware("http")
async def security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    # Skip CSP on /api/* — only HTML pages need it, API responses are JSON
    # and a stray header here can break clients that parse it strictly.
    path = request.url.path
    if not path.startswith("/api"):
        response.headers["Content-Security-Policy"] = _CSP
    # Static-asset cache. Only set when serve_page hasn't already pinned
    # the response to no-cache (HTML pages keep no-cache, JS/CSS/SVG get
    # browser+CDN caching). Skip API responses entirely — they get
    # endpoint-specific cache policy.
    if not path.startswith("/api") and request.method == "GET":
        cc = _static_cache_for(path)
        if cc and "cache-control" not in {k.lower() for k in response.headers.keys()}:
            response.headers["Cache-Control"] = cc
    # Remove server fingerprint
    if "server" in response.headers:
        del response.headers["server"]
    return response


# ── API routes ────────────────────────────────────────────────────────────────
from backend.api.v1.router import router as api_router  # noqa: E402
app.include_router(api_router)

# Refuse to boot if any /api/admin/* route is missing the
# `Depends(get_admin_user)` guard — see backend/services/admin_guard_check.py
# for the rationale (silent IDOR if forgotten on a new endpoint).
from backend.services.admin_guard_check import assert_admin_routes_guarded  # noqa: E402
assert_admin_routes_guarded(app)

from fastapi.responses import FileResponse, RedirectResponse
from fastapi.exceptions import HTTPException
from sqlalchemy.orm import Session
from backend.db.base import get_db
from fastapi import Depends
import os

_AUTH_PAGES  = {"portfolio", "profile", "archive", "watchlist"}
_ADMIN_PAGES = {"admin", "admin-user"}

# Legacy /app → /portfolio. Old bookmarks and existing TG-bot links keep
# working forever via 301; once browsers cache the redirect, follow-up
# requests skip the round-trip.
_LEGACY_REDIRECTS = {
    "app": "/portfolio",
}

_FRONTEND_ROOT = os.path.realpath("frontend")


def _safe_frontend_path(rel: str) -> str | None:
    """Resolve `rel` under frontend/ and reject anything that escapes the
    directory via `..` traversal. Returns the absolute path or None.

    Without this, `GET /..%2F.env` would serve the entire .env file
    (SECRET_KEY, ENCRYPTION_KEY, webhook secrets, TG tokens, DB password)
    because `os.path.join("frontend", "../.env")` produces `frontend/../.env`,
    which `os.path.exists` happily resolves to the project's .env.
    """
    candidate = os.path.realpath(os.path.join(_FRONTEND_ROOT, rel))
    # candidate must be the frontend root or a descendant of it.
    if candidate != _FRONTEND_ROOT and not candidate.startswith(_FRONTEND_ROOT + os.sep):
        return None
    return candidate


@app.get("/{page:path}", include_in_schema=False)
async def serve_page(page: str, request: Request, db: Session = Depends(get_db)):
    if page.startswith("api"):
        raise HTTPException(status_code=404)
    # Reject any traversal attempt up-front — both encoded and decoded forms
    # land here as plain ".." segments after FastAPI's path conversion.
    if ".." in page.split("/"):
        raise HTTPException(status_code=404)
    # Static files (have extension) — serve directly from frontend/
    if "." in page.split("/")[-1]:
        filepath = _safe_frontend_path(page)
        if filepath and os.path.isfile(filepath):
            # Cache-bust window: 60 s on text assets so a deploy-fix
            # propagates within a minute even to clients that didn't
            # hard-reload. Images / fonts get the longer default since
            # they rarely change.
            ext = page.rsplit(".", 1)[-1].lower()
            if ext in ("js", "css", "html"):
                headers = {"Cache-Control": "public, max-age=60"}
            else:
                headers = {"Cache-Control": "public, max-age=86400"}
            return FileResponse(filepath, headers=headers)
        raise HTTPException(status_code=404)

    base = page.split("/")[0] if page else ""

    # Legacy route 301s — old /app bookmarks and TG-bot deep links.
    if base in _LEGACY_REDIRECTS and (not page.split("/")[1:] or page == base):
        target = _LEGACY_REDIRECTS[base]
        # Preserve ?query= if any (e.g. /app?ref=foo → /portfolio?ref=foo)
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(target, status_code=301)

    if not page:
        filepath = _safe_frontend_path("index.html")
    else:
        filepath = _safe_frontend_path(page + ".html")
    if filepath is None:
        raise HTTPException(status_code=404)

    if base in _AUTH_PAGES or base in _ADMIN_PAGES:
        from backend.services.auth_service import decode_token, get_user_by_id
        token = request.cookies.get("session")
        user_id = decode_token(token) if token else None
        if not user_id:
            return RedirectResponse(f"/login?next=/{page}", status_code=302)
        if base in _ADMIN_PAGES:
            user = get_user_by_id(db, user_id)
            if not user or not user.is_admin:
                # Same honeypot trap as /api/admin/*: a logged-in non-admin
                # who hits /admin or /admin-user gets blocked. Cookie path
                # only triggers on full-page navigation, so this is a
                # deliberate probe (or someone lost their admin role since
                # last session — they get unblocked manually if so).
                try:
                    if user is not None and not user.is_admin:
                        from backend.services import honeypot_service
                        ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() \
                             or (request.client.host if request.client else None)
                        honeypot_service.trip(
                            db, user, request_ip=ip,
                            request_path=request.url.path, request_method=request.method,
                            reason="admin_page_probe",
                        )
                except Exception:
                    pass
                return RedirectResponse("/portfolio", status_code=302)

    if os.path.exists(filepath):
        return FileResponse(
            filepath,
            media_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return FileResponse(
        "frontend/404.html",
        status_code=404,
        media_type="text/html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )

# ── Static frontend ───────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
