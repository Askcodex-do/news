"""Application settings, loaded from environment variables (12-factor)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root: backend/app/core/config.py -> ../../../..
REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "development"
    log_level: str = "INFO"

    # Security
    admin_api_token: str = Field(default="change-me", repr=False)

    # Database
    database_url: str = "postgresql+asyncpg://news_app:change-me@localhost:5432/news"

    # Queue
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # Frontend
    backend_internal_url: str = "http://localhost:8000"
    next_public_api_base_url: str = "http://localhost:12000"

    # Ingestion
    trust_proxy_headers: bool = False
    default_poll_interval_seconds: int = 300
    http_user_agent: str = "NewsAI/0.1 (+https://example.com/bot)"

    # AI (used from Phase 4 onward)
    ai_provider: str = "openai"
    ai_api_key: str = Field(default="", repr=False)
    ai_model: str = "gpt-4o-mini"
    ai_base_url: str = ""
    image_provider: str = ""
    image_api_key: str = Field(default="", repr=False)

    # Cost controls
    ai_max_requests_per_hour: int = 500
    ai_max_tokens_per_article: int = 4000

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @property
    def sync_database_url(self) -> str:
        """Alembic and sync tooling use the psycopg (v3) driver."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
