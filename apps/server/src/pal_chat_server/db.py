from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from sqlite3 import Connection as SQLiteConnection

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker

from pal_chat_server.config import Settings, get_settings
from pal_chat_server.models import Base

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker[Session] | None = None
SQLITE_BUSY_TIMEOUT_MS = 5_000


def ensure_sqlite_parent_dir(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return
    if url.database is None or url.database == ":memory:":
        return
    database_path = make_url(database_url).database
    assert database_path is not None
    path = make_url(database_url).database
    assert path is not None
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)


def configure_sqlite_connection(dbapi_connection: object, _: object) -> None:
    if not isinstance(dbapi_connection, SQLiteConnection):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_engine(settings: Settings | None = None) -> Engine:
    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is not None:
        return _ENGINE

    config = settings or get_settings()
    ensure_sqlite_parent_dir(config.resolved_database_url)
    engine = create_engine(
        config.resolved_database_url,
        connect_args=(
            {"check_same_thread": False}
            if config.resolved_database_url.startswith("sqlite")
            else {}
        ),
        future=True,
    )
    if config.resolved_database_url.startswith("sqlite"):
        event.listen(engine, "connect", configure_sqlite_connection)
        with engine.connect():
            pass
    _ENGINE = engine
    _SESSION_FACTORY = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
    return engine


def stamp_ready_revision(engine: Engine, revision: str) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"
        )
        count = connection.exec_driver_sql("SELECT COUNT(*) FROM alembic_version").scalar_one()
        if count == 0:
            connection.exec_driver_sql(
                "INSERT INTO alembic_version(version_num) VALUES (?)",
                (revision,),
            )
        else:
            connection.exec_driver_sql("UPDATE alembic_version SET version_num = ?", (revision,))


def ensure_catalog_schema(settings: Settings | None = None) -> None:
    config = settings or get_settings()
    engine = get_engine(config)
    Base.metadata.create_all(bind=engine)
    stamp_ready_revision(engine, config.ready_schema_revision)


def get_session_factory() -> sessionmaker[Session]:
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        get_engine()
    assert _SESSION_FACTORY is not None
    return _SESSION_FACTORY


def get_db_session() -> Iterator[Session]:
    ensure_catalog_schema(get_settings())
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def catalog_path(settings: Settings | None = None) -> Path:
    config = settings or get_settings()
    url = make_url(config.resolved_database_url)
    assert url.database is not None
    return Path(url.database)


def reset_db_state() -> None:
    global _ENGINE, _SESSION_FACTORY
    _ENGINE = None
    _SESSION_FACTORY = None
