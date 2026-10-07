from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
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
from app.models.enums import EventStatus

# Dimension of the sentence-embedding model used for clustering.
# Must match the embedding model configured for the deployment.
EMBEDDING_DIM = 1536


class Event(Base, UUIDMixin, TimestampMixin):
    """A real-world event that one or more source reports describe (spec section 8)."""

    __tablename__ = "events"

    event_type: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str | None] = mapped_column(ForeignKey("countries.code"), index=True)
    region: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(120))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    first_detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    importance_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    status: Mapped[EventStatus] = mapped_column(
        Enum(EventStatus, name="event_status"),
        default=EventStatus.DETECTED,
        index=True,
        nullable=False,
    )
    # Centroid embedding of clustered reports; used by the clustering worker.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))


class EventReport(Base, UUIDMixin, TimestampMixin):
    """Association between an event and the source reports that describe it."""

    __tablename__ = "event_reports"
    __table_args__ = (UniqueConstraint("event_id", "source_report_id", name="uq_event_report"),)

    event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("events.id"), index=True, nullable=False
    )
    source_report_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("source_reports.id"), index=True, nullable=False
    )
    similarity_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class EventFact(Base, UUIDMixin, TimestampMixin):
    """An atomic, attributed fact extracted for an event.

    `first_seen_at` / `last_confirmed_at` / `source_count` implement the
    stale-information protection in spec section 23.
    """

    __tablename__ = "event_facts"

    event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("events.id"), index=True, nullable=False
    )
    fact_type: Mapped[str] = mapped_column(String(32), default="statement", nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    # Structured value for numeric facts (e.g. casualties) so contradictions
    # between reports can be detected deterministically (spec section 24).
    value_numeric: Mapped[float | None] = mapped_column(Float)
    value_text: Mapped[str | None] = mapped_column(Text)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    independent_source_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("event_facts.id")
    )


class EventConflict(Base, UUIDMixin, TimestampMixin):
    """A disagreement between sources about the same fact (spec section 24)."""

    __tablename__ = "event_conflicts"

    event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("events.id"), index=True, nullable=False
    )
    fact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="unresolved", nullable=False)
    resolution: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EventUpdate(Base, UUIDMixin, TimestampMixin):
    """Append-only timeline of changes to an event (developing stories, section 22)."""

    __tablename__ = "event_updates"

    event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("events.id"), index=True, nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    update_type: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_report_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("source_reports.id")
    )
