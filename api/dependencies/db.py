"""Synchronous database sessions, explicitly closed after every request."""

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, event
from sqlalchemy.engine import make_url
from sqlmodel import Session, SQLModel, create_engine

from api.core.config import settings


def create_database_engine(url: str) -> Engine:
    parsed = make_url(url)
    options: dict = {"pool_pre_ping": True, "hide_parameters": True}
    if parsed.get_backend_name() == "sqlite":
        if parsed.database and parsed.database != ":memory:":
            Path(parsed.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
        options["connect_args"] = {
            "check_same_thread": False,
            "timeout": settings.DB_CONNECT_TIMEOUT,
        }
        if parsed.database and parsed.database != ":memory:":
            options.update(
                pool_size=settings.DB_POOL_SIZE,
                max_overflow=settings.DB_MAX_OVERFLOW,
                pool_timeout=settings.DB_POOL_TIMEOUT,
            )
    else:
        options.update(
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_timeout=settings.DB_POOL_TIMEOUT,
            connect_args={"connect_timeout": settings.DB_CONNECT_TIMEOUT},
        )
    engine = create_engine(url, **options)
    if parsed.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={int(settings.DB_CONNECT_TIMEOUT * 1000)}")
            cursor.close()

    return engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_database_engine(settings.DATABASE_URL)


def create_db_and_tables(engine: Engine | None = None) -> None:
    # Import registers every table even when this is called by an external tool.
    from api.models import device  # noqa: F401

    SQLModel.metadata.create_all(engine or get_engine())


def get_db_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        try:
            yield session
        except BaseException:
            session.rollback()
            raise
