from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    xdg_data_home = Path.home() / ".local" / "share"
    return xdg_data_home / "pal-chat"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PAL_CHAT_",
        env_ignore_empty=True,
        extra="ignore",
    )

    app_name: str = "pal-chat-server"
    env: str = "development"
    cors_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]
    data_dir: Path = Field(default_factory=default_data_dir)
    database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PAL_CHAT_DATABASE_URL", "database_url"),
    )
    model_provider: str = "fake"
    model_api_key: str | None = None
    ready_schema_revision: str = "20260728_0001"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        db_path = self.data_dir / "pal-chat.db"
        return f"sqlite:///{db_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
