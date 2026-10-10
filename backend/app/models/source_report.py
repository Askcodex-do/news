from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import SourceReportStatus
from app.models.event import EMBEDDING_DIM


class SourceReport(Base, UUIDMixin, TimestampMixin):
    """A single report fetched from a source (spec section 6)."""

    __tablename__ = "source_reports"
    __table_args__ = (
        # Level 1 duplicate detection: a canonical URL is ingested at most once.
        UniqueConstraint("canonical_url_hash", name="uq_source_report_canonical"),
        Index("ix_source_reports_content_hash", "content_hash"),
        Index("ix_source_reports_published_at", "published_at"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sources.id"), index=True, nullable=False
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    # SHA-256 of the canonical URL, so the uniqueness index stays bounded.
    canonical_url_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="en", nullable=False)
    author: Mapped[str | None] = mapped_column(String(200))

    # Raw body is stored ONLY when the source's terms permit it.
    raw_text_if_permitted: Mapped[str | None] = mapped_column(Text)
    # SHA-256 over normalized content (Level 2 duplicate detection).
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Cheap near-duplicate bucket key (Level 3): SimHash / MinHash band.
    simhash: Mapped[str | None] = mapped_column(String(64), index=True)

    # Phase 3 event intelligence: classified type and sentence embedding used by
    # the clustering worker to decide which event this report describes.
    event_type: Mapped[str | None] = mapped_column(String(64), index=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[SourceReportStatus] = mapped_column(
        Enum(SourceReportStatus, name="source_report_status"),
        default=SourceReportStatus.NEW,
        nullable=False,
    )
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("source_reports.id")
    )
