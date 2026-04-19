import logging
import warnings
from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from backend.logging_config import setup_logging
from settings import settings

setup_logging(settings.LOG_LEVEL)
logger = logging.getLogger("avalant")

_INSECURE_DEFAULTS = {
    "change-me-in-production-use-a-long-random-string",
}


def _check_security():
    """Warn loudly if dangerous default secrets are in use."""
    issues = []
    if settings.SECRET_KEY in _INSECURE_DEFAULTS:
        issues.append("SECRET_KEY is using the default insecure value")
    if settings.ENCRYPTION_KEY in _INSECURE_DEFAULTS:
        issues.append("ENCRYPTION_KEY is using the default insecure value")
    if issues:
        msg = (
            "\n" + "=" * 60 +
            "\n  ⚠  SECURITY WARNING\n" +
            "\n".join(f"  • {i}" for i in issues) +
            "\n  Set these in your .env file before deploying!" +
            "\n" + "=" * 60
        )
        warnings.warn(msg, stacklevel=2)
        logger.warning(msg)


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
    _check_security()
    logger.info("Starting Avalant")
    run_migrations()
    _ensure_system_tags()
    logger.info("Migrations applied — server ready")

    from backend.services.price_service import start_price_loop, stop_price_loop
    start_price_loop()
    logger.info("Price refresh loop started")

    from backend.api.v1.screener import start_screener_broadcaster, stop_screener_broadcaster
    start_screener_broadcaster()

    from backend.services.alert_service import start_alert_service, stop_alert_service
    start_alert_service()

    from backend.services.tg_bot_service import start_tg_bot, stop_tg_bot
    start_tg_bot()

    from backend.services.orderbook_cache import start_prewarm, stop_prewarm
    start_prewarm()

    import asyncio, fcntl
    _alpha_tasks = []
    # Background loops should only run on ONE worker — use file lock
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

    yield

    for t in _alpha_tasks:
        t.cancel()
    stop_price_loop()
    stop_screener_broadcaster()
    stop_alert_service()
    stop_tg_bot()
    stop_prewarm()
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


_SCREENER_PATHS = ("/screener", "/arb")
_SCREENER_API_PREFIXES = ("/api/screener/",)
_PORTFOLIO_PATHS = ("/app", "/archive", "/profile", "/watchlist")
_PORTFOLIO_API_PREFIXES = ("/api/wallets", "/api/portfolio", "/api/alerts", "/api/trade")


def _section_maintenance_html(section: str, title: str, body: str) -> str:
    # Standalone inline page — no dependency on static assets so it works even
    # during maintenance. Matches avalant visual language.
    return f"""<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{title} · avalant_</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel=stylesheet>
<style>
html,body{{margin:0;padding:0;background:#0E0E11;color:#E6E8E3;font-family:Inter,system-ui,sans-serif;height:100%;-webkit-font-smoothing:antialiased;}}
.wrap{{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;}}
.card{{max-width:440px;width:100%;background:#131217;border:1px solid #22222A;border-radius:14px;padding:32px 28px;text-align:center;box-shadow:0 24px 60px rgba(0,0,0,0.45);}}
.icon{{width:52px;height:52px;margin:0 auto 18px;border-radius:13px;display:flex;align-items:center;justify-content:center;background:rgba(229,192,123,0.12);border:1px solid rgba(229,192,123,0.4);color:#E5C07B;}}
.section{{display:inline-block;font-size:10px;font-weight:700;letter-spacing:0.14em;padding:3px 9px;border-radius:5px;background:rgba(26,255,171,0.08);color:#1AFFAB;border:1px solid rgba(26,255,171,0.3);text-transform:uppercase;margin-bottom:10px;}}
h1{{margin:0 0 10px;font-size:21px;letter-spacing:-0.01em;font-weight:700;}}
p{{margin:0 0 22px;color:#9B9FAB;font-size:13.5px;line-height:1.55;}}
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
  <p>{body}</p>
  <a class=cta href="/">Back to home</a>
  <div class=brand>avalant<span>_</span></div>
</div></div>
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
        # Allow monitor hits + static assets needed by the maintenance page
        allow = path in ("/maintenance", "/maintenance.html") or \
                path.startswith(_MAINT_BYPASS_PREFIXES) or \
                path.startswith("/api/admin/")
        if not allow:
            return _FR(
                "frontend/maintenance.html",
                status_code=503,
                media_type="text/html",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Retry-After": "600",
                },
            )

    # Per-section soft maintenance — /api/admin/* always stays reachable so
    # the toggle can be flipped back off.
    if not path.startswith("/api/admin/"):
        try:
            from backend.services import admin_settings
            if admin_settings.is_screener_disabled() and _is_screener_path(path):
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Screener temporarily disabled"}, status_code=503)
                return HTMLResponse(
                    _section_maintenance_html(
                        "Screener",
                        "Screener temporarily unavailable",
                        "We're doing scheduled maintenance on the screener and arbitrage pages. Funding rates and pair pages will be back shortly.",
                    ),
                    status_code=503,
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Retry-After": "600"},
                )
            if admin_settings.is_portfolio_disabled() and _is_portfolio_path(path):
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Portfolio temporarily disabled"}, status_code=503)
                return HTMLResponse(
                    _section_maintenance_html(
                        "Portfolio",
                        "Portfolio temporarily unavailable",
                        "Your portfolio, balance fetches, and alerts are paused for maintenance. The screener stays available.",
                    ),
                    status_code=503,
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Retry-After": "600"},
                )
        except Exception:
            pass

    return await call_next(request)


# ── Security headers ──────────────────────────────────────────────────────────
@app.middleware("http")
async def security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    # Remove server fingerprint
    if "server" in response.headers:
        del response.headers["server"]
    return response


# ── API routes ────────────────────────────────────────────────────────────────
from backend.api.v1.router import router as api_router  # noqa: E402
app.include_router(api_router)

from fastapi.responses import FileResponse, RedirectResponse
from fastapi.exceptions import HTTPException
from sqlalchemy.orm import Session
from backend.db.base import get_db
from fastapi import Depends
import os

_AUTH_PAGES  = {"app", "profile", "archive", "watchlist"}
_ADMIN_PAGES = {"admin", "admin-user"}

@app.get("/{page:path}", include_in_schema=False)
async def serve_page(page: str, request: Request, db: Session = Depends(get_db)):
    if page.startswith("api"):
        raise HTTPException(status_code=404)
    # Static files (have extension) — serve directly from frontend/
    if "." in page.split("/")[-1]:
        filepath = os.path.join("frontend", page)
        if os.path.exists(filepath):
            return FileResponse(filepath)
        raise HTTPException(status_code=404)

    base = page.split("/")[0] if page else ""
    filepath = "frontend/index.html" if not page else os.path.join("frontend", page + ".html")

    if base in _AUTH_PAGES or base in _ADMIN_PAGES:
        from backend.services.auth_service import decode_token, get_user_by_id
        token = request.cookies.get("session")
        user_id = decode_token(token) if token else None
        if not user_id:
            return RedirectResponse(f"/login?next=/{page}", status_code=302)
        if base in _ADMIN_PAGES:
            user = get_user_by_id(db, user_id)
            if not user or not user.is_admin:
                return RedirectResponse("/app", status_code=302)

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
