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

    # Embeddings (Phase 3 clustering). The default hashing provider runs fully
    # offline and deterministically; set provider=openai to use a real model.
    embedding_provider: str = "hashing"
    embedding_model: str = "text-embedding-3-small"
    embedding_api_key: str = Field(default="", repr=False)
    embedding_dim: int = 1536
    # Embed at most this many reports per clustering pass (cost control).
    embedding_max_per_pass: int = 200

    # Clustering / event identity (spec sections 8-9)
    # Combined similarity above which two reports are treated as the same event.
    # Below the semantic weight so a strong headline match clears it alone.
    cluster_similarity_threshold: float = 0.70
    # Multiplier applied when two known event types disagree. A penalty rather
    # than a veto, because keyword type labels are imperfect. Kept high enough
    # that an identical headline still clears the threshold across labels.
    cluster_type_mismatch_penalty: float = 0.90
    # Cosine similarity above which two reports are considered near-duplicates
    # for the purpose of collapsing them into one independent source.
    near_duplicate_threshold: float = 0.92
    # A new report may join an existing event only if its publish time is within
    # this window of the event's activity (events are time-bounded).
    event_time_window_hours: int = 72

    # Verification / confidence (spec sections 10-13)
    min_confidence_to_publish: float = 70.0
    # Independent-source counts mapped onto the confidence scale.
    confidence_independent_target: int = 5
    # Relative tolerance before two numeric claims count as a conflict.
    conflict_relative_tolerance: float = 0.05

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
