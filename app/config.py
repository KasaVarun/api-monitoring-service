"""Application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///./data/monitor.db"
    request_timeout_seconds: float = Field(default=10.0, ge=1, le=120)
    min_check_interval_seconds: int = Field(default=10, ge=1)
    max_check_interval_seconds: int = Field(default=3600, ge=1)
    default_check_interval_seconds: int = Field(default=60, ge=1)
    check_concurrency: int = Field(default=10, ge=1, le=100)
    history_retention_days: int = Field(default=7, ge=1, le=365)
    history_max_records_per_endpoint: int = Field(default=1000, ge=10)
    worker_poll_seconds: float = Field(default=1.0, ge=0.05, le=30)
    manual_check_wait_seconds: float = Field(default=20.0, ge=1, le=120)
    max_response_bytes: int = Field(default=8192, ge=0, le=1_000_000)
    log_level: str = "INFO"

    def sqlite_path(self) -> Path | None:
        url = self.database_url
        prefix = "sqlite+aiosqlite:///"
        if not url.startswith(prefix):
            return None
        rest = url.removeprefix(prefix)
        if rest.startswith("/"):
            return Path(rest)
        return Path(rest)

    def ensure_sqlite_dir(self) -> None:
        path = self.sqlite_path()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
