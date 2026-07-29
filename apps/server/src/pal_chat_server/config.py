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
    app_version: str = "0.2.0"
    env: str = "development"
    cors_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]
    data_dir: Path = Field(default_factory=default_data_dir)
    database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PAL_CHAT_DATABASE_URL", "database_url"),
    )
    ready_schema_revision: str = "20260729_0001"
    bind_host: str = "127.0.0.1"
    keyring_service_name: str = "pal-chat"
    credential_backend: str = "keyring"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        db_path = self.data_dir / "catalog.sqlite"
        return f"sqlite:///{db_path}"

    @property
    def experiments_dir(self) -> Path:
        return self.data_dir / "experiments"


@lru_cache
def get_settings() -> Settings:
    return Settings()
