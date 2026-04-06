"""Centralized logging configuration for Avalant."""
import logging
import logging.config
import os


def setup_logging(log_level: str = "INFO") -> None:
    """Configure application-wide logging.

    Two rotating file handlers:
      - logs/app.log   — INFO and above (all activity)
      - logs/errors.log — ERROR and above (failures only)

    Console mirrors the same level as log_level.
    Noisy third-party libraries are silenced to WARNING.
    """
    os.makedirs("logs", exist_ok=True)

    level = log_level.upper()

    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "brief": {
                "format": "%(asctime)s [%(levelname)s] %(message)s",
                "datefmt": "%H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "brief",
                "level": level,
            },
            "file_app": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": "logs/app.log",
                "maxBytes": 10 * 1024 * 1024,  # 10 MB
                "backupCount": 5,
                "formatter": "standard",
                "level": "INFO",
                "encoding": "utf-8",
            },
            "file_errors": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": "logs/errors.log",
                "maxBytes": 10 * 1024 * 1024,  # 10 MB
                "backupCount": 10,
                "formatter": "standard",
                "level": "ERROR",
                "encoding": "utf-8",
            },
        },
        "loggers": {
            # Application loggers
            "avalant": {
                "handlers": ["console", "file_app", "file_errors"],
                "level": level,
                "propagate": False,
            },
            # Suppress noisy third-party libs
            "httpx": {"level": "WARNING", "propagate": True},
            "httpcore": {"level": "WARNING", "propagate": True},
            "uvicorn.access": {"level": "WARNING", "propagate": True},
            "sqlalchemy.engine": {"level": "WARNING", "propagate": True},
            "alembic": {"level": "INFO", "propagate": True},
        },
        "root": {
            "handlers": ["console", "file_app", "file_errors"],
            "level": level,
        },
    }

    logging.config.dictConfig(config)
