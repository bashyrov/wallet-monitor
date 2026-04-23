from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from settings import settings


def _make_engine():
    url = settings.DATABASE_URL
    # Heroku / Railway use "postgres://" — SQLAlchemy 2.x requires "postgresql://"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 10
        # Recycle every 30 min so SQLAlchemy's own pool drops connections
        # before pgbouncer reaps them for `server_lifetime` (default 60 min).
        # Without this, SQLAlchemy occasionally checks out a connection that
        # pgbouncer has already closed upstream → "server closed connection
        # unexpectedly" on the next query. pool_pre_ping catches most of these
        # but a race remains when pgbouncer closes mid-transaction.
        kwargs["pool_recycle"] = 1800
    return create_engine(url, **kwargs)


engine = _make_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from backend.db import models  # noqa: register models with Base
    Base.metadata.create_all(bind=engine)
