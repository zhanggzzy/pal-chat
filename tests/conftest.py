from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pal_chat_server.app import create_app
from pal_chat_server.config import get_settings
from pal_chat_server.db import reset_db_state

ROOT_DIR = Path(__file__).resolve().parents[1]


def configure_test_env(data_dir: Path) -> Path:
    db_path = data_dir / "catalog.sqlite"
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["PAL_CHAT_DATA_DIR"] = str(data_dir)
    os.environ["PAL_CHAT_DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["PAL_CHAT_CREDENTIAL_BACKEND"] = "memory"
    get_settings.cache_clear()
    reset_db_state()
    return db_path


def clear_test_env() -> None:
    os.environ.pop("PAL_CHAT_DATA_DIR", None)
    os.environ.pop("PAL_CHAT_DATABASE_URL", None)
    os.environ.pop("PAL_CHAT_CREDENTIAL_BACKEND", None)
    get_settings.cache_clear()
    reset_db_state()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def migrated_app(data_dir: Path) -> Generator[TestClient, None, None]:
    db_path = configure_test_env(data_dir)

    config = Config(str(ROOT_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")

    app = create_app()
    with TestClient(app) as client:
        yield client

    clear_test_env()


@pytest.fixture
def unmigrated_app(data_dir: Path) -> Generator[TestClient, None, None]:
    configure_test_env(data_dir)

    app = create_app()
    with TestClient(app) as client:
        yield client

    clear_test_env()
