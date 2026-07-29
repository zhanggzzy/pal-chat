from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn
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
    os.environ.pop("PAL_CHAT_SERVER_BASE_URL", None)
    get_settings.cache_clear()
    reset_db_state()


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


@dataclass
class LiveServer:
    base_url: str
    client: httpx.Client
    app: object


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


@pytest.fixture
def live_process_server(data_dir: Path) -> Generator[LiveServer, None, None]:
    db_path = configure_test_env(data_dir)

    config = Config(str(ROOT_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    os.environ["PAL_CHAT_SERVER_BASE_URL"] = base_url
    get_settings.cache_clear()

    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    with httpx.Client(base_url=base_url, timeout=10) as client:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                response = client.get("/health/live")
            except httpx.HTTPError:
                time.sleep(0.05)
                continue
            if response.status_code == 200:
                break
        else:
            server.should_exit = True
            thread.join(timeout=5)
            raise RuntimeError("Live test server failed to start.")
        yield LiveServer(base_url=base_url, client=client, app=app)

    server.should_exit = True
    thread.join(timeout=5)
    clear_test_env()
