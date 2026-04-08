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

    yield

    stop_price_loop()
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

from fastapi.responses import FileResponse
from fastapi.exceptions import HTTPException
import os

@app.get("/{page:path}", include_in_schema=False)
async def serve_page(page: str):
    if page.startswith("api") or "." in page.split("/")[-1]:
        raise HTTPException(status_code=404)
    if not page:
        filepath = "frontend/index.html"
    else:
        filepath = os.path.join("frontend", page + ".html")
    if os.path.exists(filepath):
        return FileResponse(
            filepath,
            media_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    raise HTTPException(status_code=404)

# ── Static frontend ───────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
