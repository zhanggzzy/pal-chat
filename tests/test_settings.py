from __future__ import annotations

import pytest
from pal_chat_server.config import Settings


def test_empty_env_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAL_CHAT_CORS_ORIGINS", "")
    monkeypatch.setenv("PAL_CHAT_DATABASE_URL", "")

    settings = Settings()

    assert settings.cors_origins == ["http://127.0.0.1:5173", "http://localhost:5173"]
    assert settings.database_url is None
