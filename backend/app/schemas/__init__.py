"""Pydantic response/request schemas for the public and admin APIs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.enums import EventStatus, JobStatus, SourceType


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    country: str | None
    type: SourceType
    website_url: str
    language: str
    priority: int
    enabled: bool
    reliability_score: float
    is_international: bool
    last_success_at: datetime | None
    last_failure_at: datetime | None


class SourceHealthOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_id: uuid.UUID
    status: str
    consecutive_failures: int
    success_count: int
    failure_count: int
    last_checked_at: datetime | None
    avg_latency_ms: float | None
    last_error: str | None


class CountrySourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    country_code: str
    role: str
    priority: int
    enabled: bool
    source_slug: str
    source_name: str


class ArticleCard(BaseModel):
    """Compact representation used in feed lists."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    headline: str
    subtitle: str | None
    category: str | None
    location: str | None
    confidence_score: float
    importance_score: float
    published_at: datetime | None


class SourceAttribution(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_name: str
    url: str
    is_independent: bool


class ArticleImageOut(BaseModel):
    """Image *metadata* only — never image bytes (spec section 20)."""

    model_config = ConfigDict(from_attributes=True)

    image_provider: str
    generation_id: str | None
    prompt_hash: str
    ephemeral_url: str | None
    expires_at: datetime | None


class ArticleDetail(ArticleCard):
    body: str
    key_points: list | None
    timeline: list | None
    seo_title: str | None
    seo_description: str | None
    current_version: int
    sources: list[SourceAttribution] = []
    image: ArticleImageOut | None = None

    @field_validator("key_points", "timeline", mode="before")
    @classmethod
    def _decode_json_column(cls, value: object) -> object:
        """Decode ``key_points``/``timeline`` from their JSON-text storage.

        The ``Article`` model stores both as JSON-encoded ``Text``, but the API
        exposes lists. Without this, ``model_validate`` sees the raw string and
        raises, 500ing the detail endpoint that renders a published article.
        """
        if not isinstance(value, str):
            return value
        try:
            decoded = json.loads(value)
        except ValueError:
            return None
        return decoded if isinstance(decoded, list) else None


class FeedResponse(BaseModel):
    country_code: str
    resolved: bool
    global_articles: list[ArticleCard]
    local_articles: list[ArticleCard]
    local_source_name: str | None = None


class GeoResponse(BaseModel):
    country_code: str
    resolved: bool


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    event_type: str | None
    country: str | None
    status: EventStatus
    confidence_score: float
    importance_score: float
    first_detected_at: datetime
    last_updated_at: datetime


class JobOut(BaseModel):
    """A background job, exposed for operational visibility (spec section 33)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_type: str
    idempotency_key: str
    status: JobStatus
    attempts: int
    max_attempts: int
    run_after: datetime | None
    locked_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class SourceHealthRow(BaseModel):
    """Flattened source + health view for the ops dashboard."""

    source_slug: str
    source_name: str
    enabled: bool
    is_international: bool
    status: str
    consecutive_failures: int
    success_count: int
    failure_count: int
    avg_latency_ms: float | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error: str | None


class IngestionStatsOut(BaseModel):
    """Aggregate ingestion counters (spec section 33)."""

    reports_total: int
    reports_last_hour: int
    reports_last_24h: int
    duplicates_total: int
    sources_healthy: int
    sources_degraded: int
    sources_down: int
    sources_unknown: int
    queue: dict[str, int]
    generated_at: datetime


class SourceHealthSummaryOut(BaseModel):
    total: int
    enabled: int
    healthy: int
    degraded: int
    down: int
    unknown: int
    offline: list[str]


class IngestionSummaryOut(BaseModel):
    reports_total: int
    reports_last_hour: int
    reports_last_24h: int
    duplicates_total: int
    failed_reports_total: int


class EventSummaryOut(BaseModel):
    total: int
    by_status: dict[str, int]
    created_last_24h: int
    merged_reports_total: int


class JobSummaryOut(BaseModel):
    queue: dict[str, int]
    dead_total: int
    failures_last_hour: int
    max_attempts_seen: int


class CostSummaryOut(BaseModel):
    """AI spend guard state (spec sections 33-34)."""

    ai_calls_used_this_hour: int
    ai_calls_limit_per_hour: int
    ai_budget_remaining: int
    ai_budget_exhausted: bool


class OpsMetricsOut(BaseModel):
    """Operational snapshot (spec section 33)."""

    generated_at: datetime
    sources: SourceHealthSummaryOut
    ingestion: IngestionSummaryOut
    events: EventSummaryOut
    jobs: JobSummaryOut
    scheduler: dict | None
    articles_published: int
    cost: CostSummaryOut


class AccuracyMetricsOut(BaseModel):
    """Accuracy dashboard (spec section 33)."""

    generated_at: datetime
    articles_published: int
    articles_published_24h: int
    fact_validation_failures: int
    corrections: int
    source_conflicts_open: int
    low_confidence_publications: int
    duplicate_publications: int
    rejection_rate: float
    confidence_floor: float


class EventFactOut(BaseModel):
    """A verified fact with its source support (spec section 10)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    fact_type: str
    fact_key: str | None
    statement: str
    value_numeric: float | None
    value_text: str | None
    source_count: int
    independent_source_count: int
    first_seen_at: datetime
    last_confirmed_at: datetime | None
    superseded_by_id: uuid.UUID | None


class EventConflictOut(BaseModel):
    """A source disagreement (spec section 24)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    fact_type: str
    claim: str
    status: str
    resolution: str | None
    detected_at: datetime
    resolved_at: datetime | None


class EventDetailOut(EventOut):
    """An event plus the evidence the AI writer is allowed to use."""

    report_count: int
    independent_source_count: int
    conflict_count: int
    facts: list[EventFactOut]
    conflicts: list[EventConflictOut]


class EventIntelligenceStatsOut(BaseModel):
    """Accuracy dashboard for Phase 3 (spec section 33)."""

    events_total: int
    events_by_status: dict[str, int]
    events_verified: int
    events_unverified: int
    events_with_conflicts: int
    facts_total: int
    conflicts_total: int
    conflicts_unresolved: int
    mean_confidence: float | None
    low_confidence_published: int
    generated_at: datetime
