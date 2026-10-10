"""Application settings, loaded from environment variables (12-factor)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
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

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """Force the asyncpg driver onto a plain Postgres URL.

        Managed providers (Render, Heroku) hand out ``postgresql://`` or
        ``postgres://``; SQLAlchemy would then pick a sync dialect that the
        async engine cannot use. Normalize here so ``DATABASE_URL`` can be set
        verbatim from the provider. ``sync_database_url`` later swaps in psycopg.
        """
        for scheme in ("postgresql://", "postgres://"):
            if value.startswith(scheme):
                return "postgresql+asyncpg://" + value[len(scheme) :]
        return value

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
    image_model: str = "gpt-image-1"
    image_base_url: str = ""
    # Images are optional: disabled means no image jobs are enqueued and
    # articles publish without one (spec section 27).
    image_enabled: bool = True
    # How long a transient provider URL is considered valid before its
    # reference is purged. We never store image bytes (spec section 20).
    image_url_ttl_seconds: int = 3600

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

    # AI editorial system (spec sections 16-19, 29)
    # Generate an article only for events at/above this importance, even if
    # confidence is high: a confirmed trivial event is not worth publishing.
    min_importance_to_publish: float = 30.0
    # Fraction of a draft's 8-grams allowed to match one source report verbatim
    # before the draft is rejected as copying (spec section 17).
    max_source_copy_overlap: float = 0.30
    # Re-run the AI fact-check pass on every draft (spec section 28, layer 5).
    ai_fact_check_enabled: bool = True

    # Localization / feed (spec sections 5, 18, 21)
    # Max stories per feed. The primary feed shows at most 20 + 20 (section 21).
    feed_limit: int = 20
    # Recency half-life for ranking; an article's recency term halves every
    # this many hours. Importance dominates, recency only breaks ties.
    rank_recency_half_life_hours: float = 18.0
    # Multiplier applied to local-feed candidates so a country's own coverage
    # rises slightly. Kept small so it cannot outrank a major global story.
    local_relevance_bonus: float = 1.10
    # GeoIP backend: "null" (global only), "static" (test/dev CIDR map) or
    # "maxmind" (a GeoLite2 database path in GEOIP_DATABASE_PATH).
    geoip_provider: str = "null"
    geoip_database_path: str = ""
    # CIDR -> country JSON map for the "static" dev/test provider.
    geoip_static_map: str = ""

    # Continuous operation (spec section 25)
    # Seconds between a worker's scheduling scan and its execution ticks.
    worker_tick_seconds: int = 10
    scheduler_interval_seconds: int = 60
    # How long a worker's scheduling lease is valid before another may take over.
    scheduler_lease_seconds: int = 300
    # A developing event with no new reports for this long is archived.
    stale_event_hours: int = 168
    # SUCCEEDED jobs older than this are pruned; dead jobs are kept for diagnosis.
    job_retention_days: int = 7
    # How often the maintenance sweep runs, in seconds.
    maintenance_interval_seconds: int = 900

    # Cost controls (spec section 33/34)
    ai_max_requests_per_hour: int = 500
    ai_max_tokens_per_article: int = 4000
    # Whether the hourly AI budget is enforced (0 disables the cap).
    ai_budget_enabled: bool = True

    # Rate limiting (spec section 34)
    rate_limit_enabled: bool = True
    rate_limit_requests_per_minute: int = 120
    # Admin/ops endpoints are stricter than public reads.
    rate_limit_admin_requests_per_minute: int = 30
    rate_limit_window_seconds: int = 60
    # Share one counter across replicas via Redis. Off => per-process limiter.
    rate_limit_redis_enabled: bool = False
    # Trust X-Forwarded-For for the client address used to key the limiter and
    # to resolve a country. Only enable behind a proxy you control.
    # (shares TRUST_PROXY_HEADERS above)

    # Outbound fetch hardening (spec section 34: validate all external URLs)
    # Refuse to fetch feeds that resolve to private/loopback/link-local hosts,
    # so a misconfigured source cannot be used for SSRF.
    block_private_fetch_hosts: bool = True

    # Security headers (spec section 34)
    # HSTS is only meaningful over HTTPS; off by default for local HTTP dev.
    enable_hsts: bool = False
    hsts_max_age_seconds: int = 31536000
    content_security_policy: str = (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )

    # Backups / disaster recovery (spec section 36)
    backup_dir: str = "backups"
    backup_retention_days: int = 14

    # Fail startup on a production misconfiguration instead of only logging it.
    strict_config: bool = False

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    def production_problems(self) -> list[str]:
        """Return misconfigurations that must not ship to production.

        Called at startup; a non-empty list is logged (and, when
        ``STRICT_CONFIG`` is on, fatal). This makes "keep API keys in
        environment variables, never expose them" a checked property rather
        than a convention (spec section 34).
        """
        problems: list[str] = []
        if not self.is_production:
            return problems
        weak_tokens = {"change-me", "testadmin", "dev-admin-token"}
        if not self.admin_api_token or self.admin_api_token in weak_tokens:
            problems.append("ADMIN_API_TOKEN is unset or a well-known default")
        if "change-me" in self.database_url:
            problems.append("DATABASE_URL still contains the default password")
        if not self.trust_proxy_headers:
            problems.append(
                "TRUST_PROXY_HEADERS is false; visitor country resolution will use "
                "the proxy address instead of the client"
            )
        return problems

    @property
    def sync_database_url(self) -> str:
        """Alembic and sync tooling use the psycopg (v3) driver."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
