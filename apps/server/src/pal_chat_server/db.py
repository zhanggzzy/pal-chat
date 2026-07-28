from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pal_chat_server.config import Settings, get_settings

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker[Session] | None = None


def get_engine(settings: Settings | None = None) -> Engine:
    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is not None:
        return _ENGINE

    config = settings or get_settings()
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
        config.data_dir.mkdir(parents=True, exist_ok=True)
        with engine.begin() as connection:
            connection.execute(text("PRAGMA journal_mode=WAL"))
            connection.execute(text("PRAGMA foreign_keys=ON"))
    _ENGINE = engine
    _SESSION_FACTORY = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
    return engine


def get_session_factory() -> sessionmaker[Session]:
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        get_engine()
    assert _SESSION_FACTORY is not None
    return _SESSION_FACTORY


def get_db_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def reset_db_state() -> None:
    global _ENGINE, _SESSION_FACTORY
    _ENGINE = None
    _SESSION_FACTORY = None
