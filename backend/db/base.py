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
