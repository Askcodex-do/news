"""Pydantic response/request schemas for the public and admin APIs."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

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


class ArticleDetail(ArticleCard):
    body: str
    key_points: list | None
    timeline: list | None
    seo_title: str | None
    seo_description: str | None
    current_version: int
    sources: list[SourceAttribution] = []


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
