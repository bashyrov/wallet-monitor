"""Centralised logging for every Avalant process (web + fetcher).

Goals:
  · identical console format in all roles (so `docker logs` stays readable)
  · rotating file handlers under <LOG_DIR>/<role>/ so we can grep errors
    long after they rolled out of the docker buffer
  · separate errors.log (WARNING+) for fast triage
  · uncaught exception hooks: sys.excepthook + asyncio default exception
    handler + threading.excepthook — nothing should die silently

Call `setup_logging(role)` once at process start before any logger.* call.
The log dir defaults to /var/log/avalant (mounted volume in docker) and
falls back to /tmp/avalant_logs locally.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path


def _default_log_dir() -> Path:
    env = os.environ.get("AVALANT_LOG_DIR")
    if env:
        return Path(env)
    # /var/log/avalant exists in the container (mounted volume); fall back
    # to /tmp locally for developers who run uvicorn outside docker.
    candidate = Path("/var/log/avalant")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        if os.access(candidate, os.W_OK):
            return candidate
    except OSError:
        pass
    return Path("/tmp/avalant_logs")


_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 10 MB × 5 files per channel = 50 MB cap per role
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5

_configured = False
_log_dir_used: Path | None = None


def get_log_dir() -> Path | None:
    """Return the directory currently used for file logging, or None if
    setup_logging hasn't run yet."""
    return _log_dir_used


def setup_logging(role: str = "monolith", *, level: str | None = None) -> Path | None:
    """Wire up console + rotating file handlers for this process.

    Returns the directory where log files live (or None if file logging
    couldn't be initialised — console logging still works).
    Safe to call more than once — subsequent calls are no-ops.
    """
    global _configured, _log_dir_used
    if _configured:
        return _log_dir_used

    level_name = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    lvl = getattr(logging, level_name, logging.INFO)

    root_dir = _default_log_dir()
    role_dir: Path | None = root_dir / role
    try:
        role_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Fall back to stderr-only logging if the log dir is unwritable.
        # Better to keep running than refuse to start.
        print(f"[logging_config] cannot create {role_dir}: {exc}", file=sys.stderr)
        role_dir = None

    root = logging.getLogger()
    root.setLevel(lvl)
    # Clear any handler basicConfig installed earlier.
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(lvl)
    console.setFormatter(formatter)
    root.addHandler(console)

    if role_dir is not None:
        full = logging.handlers.RotatingFileHandler(
            role_dir / "full.log", maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        full.setLevel(lvl)
        full.setFormatter(formatter)
        root.addHandler(full)

        errors = logging.handlers.RotatingFileHandler(
            role_dir / "errors.log", maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        errors.setLevel(logging.WARNING)
        errors.setFormatter(formatter)
        root.addHandler(errors)

    # Quiet the chattiest third-party loggers.
    for name in ("httpx", "httpcore", "urllib3",
                 "websockets.client", "websockets.server",
                 "uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)

    _install_global_hooks(role)

    _log_dir_used = role_dir
    logging.getLogger("avalant").info(
        "Logging initialised (role=%s, level=%s, dir=%s)",
        role, level_name, role_dir or "stderr-only",
    )
    _configured = True
    return role_dir


def _install_global_hooks(role: str) -> None:
    """Route every uncaught exception into the log files."""
    log = logging.getLogger(f"avalant.unhandled.{role}")

    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook

    def _thread_excepthook(args):  # threading.ExceptHookArgs
        if issubclass(args.exc_type, SystemExit):
            return
        log.critical(
            "Uncaught exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_excepthook


def install_asyncio_hook() -> None:
    """Install an asyncio loop exception handler. MUST be called from inside
    a running loop (e.g. first line of the lifespan / _run coroutine)."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    log = logging.getLogger("avalant.unhandled.asyncio")

    def _handler(lp, context):
        exc = context.get("exception")
        msg = context.get("message", "asyncio error")
        if exc is not None:
            log.error("asyncio: %s", msg, exc_info=(type(exc), exc, exc.__traceback__))
        else:
            log.error("asyncio: %s (context=%r)", msg, context)

    loop.set_exception_handler(_handler)
