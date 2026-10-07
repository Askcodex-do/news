from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import SourceType


class Source(Base, UUIDMixin, TimestampMixin):
    """A news publisher / feed. Seeded from config/*.yaml, never hard-coded."""

    __tablename__ = "sources"

    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    country: Mapped[str | None] = mapped_column(ForeignKey("countries.code"), index=True)
    type: Mapped[SourceType] = mapped_column(Enum(SourceType, name="source_type"), nullable=False)
    rss_url: Mapped[str | None] = mapped_column(Text)
    website_url: Mapped[str] = mapped_column(Text, nullable=False)
    api_url: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="en", nullable=False)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reliability_score: Mapped[float] = mapped_column(Float, default=70.0, nullable=False)
    # True for the 30 international feeds; local sources are mapped per country.
    is_international: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Source lineage (spec section 11): a wire/agency root shared by republishers.
    # Sources pointing at the same root are NOT independent confirmations.
    lineage_root_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sources.id"), index=True
    )


class SourceHealth(Base, UUIDMixin, TimestampMixin):
    """Rolling operational health of a source (spec section 33 observability)."""

    __tablename__ = "source_health"

    source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sources.id"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    avg_latency_ms: Mapped[float | None] = mapped_column(Float)
    last_error: Mapped[str | None] = mapped_column(Text)


class SourceReliability(Base, UUIDMixin, TimestampMixin):
    """Learned reliability, kept separate from the configured editorial prior."""

    __tablename__ = "source_reliability"

    source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sources.id"), unique=True, nullable=False
    )
    score: Mapped[float] = mapped_column(Float, default=70.0, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accuracy_rate: Mapped[float | None] = mapped_column(Float)
    independence_weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class CountrySource(Base, UUIDMixin, TimestampMixin):
    """country -> local source mapping (spec section 4)."""

    __tablename__ = "country_sources"
    __table_args__ = (UniqueConstraint("country_code", "source_id", name="uq_country_source"),)

    country_code: Mapped[str] = mapped_column(
        ForeignKey("countries.code"), index=True, nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sources.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), default="primary", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
