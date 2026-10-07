from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import JobStatus


class ProcessingJob(Base, UUIDMixin, TimestampMixin):
    """A unit of background work with an idempotency key (spec section 26).

    The unique constraint on `idempotency_key` guarantees a crashed worker
    retrying the same logical operation cannot create a second article.
    """

    __tablename__ = "processing_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_processing_job_key"),
        Index("ix_processing_jobs_status", "status", "run_after"),
    )

    job_type: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    # e.g. "generate_article:event_18472:version_3"
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    payload: Mapped[str | None] = mapped_column(Text)  # JSON-encoded
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status"), default=JobStatus.PENDING, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(120))
    last_error: Mapped[str | None] = mapped_column(Text)
