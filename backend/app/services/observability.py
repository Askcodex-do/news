"""Operational and accuracy metrics (spec section 33).

Two read-only aggregations over the existing schema:

* :func:`ops_snapshot` — the "is the pipeline healthy right now" view: sources
  online/offline, ingestion throughput, queue depth and job outcomes.
* :func:`accuracy_snapshot` — the "can we trust what we published" view:
  published articles, validation rejections, corrections (article versions
  beyond the first), source conflicts, and low-confidence publications.

Everything is a COUNT/GROUP BY, so it is cheap enough to poll from a dashboard.
Nothing here mutates state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.article import Article, ArticleVersion
from app.models.enums import EventStatus, JobStatus, SourceReportStatus
from app.models.event import Event, EventConflict, EventReport
from app.models.job import ProcessingJob
from app.models.source import Source, SourceHealth
from app.models.source_report import SourceReport
from app.services import queue


@dataclass
class SourceHealthSummary:
    total: int = 0
    enabled: int = 0
    healthy: int = 0
    degraded: int = 0
    down: int = 0
    unknown: int = 0
    offline: list[str] = field(default_factory=list)


@dataclass
class IngestionSummary:
    reports_total: int = 0
    reports_last_hour: int = 0
    reports_last_24h: int = 0
    duplicates_total: int = 0
    failed_reports_total: int = 0


@dataclass
class EventSummary:
    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    created_last_24h: int = 0
    merged_reports_total: int = 0


@dataclass
class JobSummary:
    queue: dict[str, int] = field(default_factory=dict)
    dead_total: int = 0
    failures_last_hour: int = 0
    max_attempts_seen: int = 0


@dataclass
class OpsSnapshot:
    generated_at: datetime
    sources: SourceHealthSummary
    ingestion: IngestionSummary
    events: EventSummary
    jobs: JobSummary
    scheduler: dict | None
    articles_published: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class AccuracySnapshot:
    generated_at: datetime
    articles_published: int = 0
    articles_published_24h: int = 0
    fact_validation_failures: int = 0
    corrections: int = 0
    source_conflicts_open: int = 0
    low_confidence_publications: int = 0
    duplicate_publications: int = 0
    rejection_rate: float = 0.0
    confidence_floor: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


async def _count(session: AsyncSession, stmt) -> int:
    return int((await session.execute(stmt)).scalar_one())


async def _scalar_count(session: AsyncSession, model, *where) -> int:
    stmt = select(func.count()).select_from(model)
    if where:
        stmt = stmt.where(*where)
    return int((await session.execute(stmt)).scalar_one())


async def ops_snapshot(session: AsyncSession, *, now: datetime | None = None) -> OpsSnapshot:
    """Aggregate the current operational picture (spec section 33)."""
    now = now or datetime.now(UTC)

    source_rows = (
        await session.execute(
            select(Source.enabled, SourceHealth.status, func.count())
            .select_from(Source)
            .outerjoin(SourceHealth, SourceHealth.source_id == Source.id)
            .group_by(Source.enabled, SourceHealth.status)
        )
    ).all()
    sources = SourceHealthSummary()
    for enabled, status, count in source_rows:
        sources.total += count
        if enabled:
            sources.enabled += count
        bucket = (status or "unknown").lower()
        if bucket in {"healthy", "degraded", "down", "unknown"}:
            setattr(sources, bucket, getattr(sources, bucket) + count)

    offline = (
        (
            await session.execute(
                select(Source.name)
                .join(SourceHealth, SourceHealth.source_id == Source.id)
                .where(Source.enabled.is_(True), SourceHealth.status == "down")
                .order_by(Source.name)
            )
        )
        .scalars()
        .all()
    )
    sources.offline = list(offline)

    ingestion = IngestionSummary(
        reports_total=await _scalar_count(session, SourceReport),
        reports_last_hour=await _scalar_count(
            session, SourceReport, SourceReport.retrieved_at >= now - timedelta(hours=1)
        ),
        reports_last_24h=await _scalar_count(
            session, SourceReport, SourceReport.retrieved_at >= now - timedelta(hours=24)
        ),
        duplicates_total=await _scalar_count(
            session, SourceReport, SourceReport.status == SourceReportStatus.DUPLICATE
        ),
        failed_reports_total=await _scalar_count(
            session, SourceReport, SourceReport.status == SourceReportStatus.FAILED
        ),
    )

    status_rows = (
        await session.execute(select(Event.status, func.count()).group_by(Event.status))
    ).all()
    events = EventSummary(
        total=sum(count for _, count in status_rows),
        by_status={str(status): count for status, count in status_rows},
        created_last_24h=await _scalar_count(
            session, Event, Event.first_detected_at >= now - timedelta(hours=24)
        ),
        merged_reports_total=await _scalar_count(session, EventReport),
    )

    jobs = JobSummary(
        queue=await queue.queue_depth(session),
        dead_total=await _scalar_count(
            session, ProcessingJob, ProcessingJob.status == JobStatus.DEAD
        ),
        failures_last_hour=await _scalar_count(
            session,
            ProcessingJob,
            ProcessingJob.attempts > 1,
            ProcessingJob.updated_at >= now - timedelta(hours=1),
        ),
        max_attempts_seen=int(
            (await session.execute(select(func.max(ProcessingJob.attempts)))).scalar() or 0
        ),
    )

    return OpsSnapshot(
        generated_at=now,
        sources=sources,
        ingestion=ingestion,
        events=events,
        jobs=jobs,
        scheduler=await queue.scheduler_status(session),
        articles_published=await _scalar_count(session, Article, Article.is_published.is_(True)),
    )


async def accuracy_snapshot(
    session: AsyncSession, *, now: datetime | None = None
) -> AccuracySnapshot:
    """Aggregate the accuracy picture (spec section 33).

    ``fact_validation_failures`` counts published articles that required more
    than one version — i.e. a draft was rejected and rewritten before it went
    live — plus events parked in editorial review with no published article.
    """
    now = now or datetime.now(UTC)

    published = await _scalar_count(session, Article, Article.is_published.is_(True))
    published_24h = await _scalar_count(
        session,
        Article,
        Article.is_published.is_(True),
        Article.published_at >= now - timedelta(hours=24),
    )
    corrected = await _scalar_count(session, ArticleVersion, ArticleVersion.version > 1)
    conflicts_open = await _scalar_count(
        session, EventConflict, EventConflict.status == "unresolved"
    )
    low_confidence = await _scalar_count(
        session,
        Article,
        Article.is_published.is_(True),
        Article.confidence_score < settings.min_confidence_to_publish,
    )
    editorial_review = await _scalar_count(
        session, Event, Event.status == EventStatus.EDITORIAL_REVIEW
    )

    # A published event with more than one published article is a duplicate
    # publication (spec section 22 wants one article per event/locale).
    dup_stmt = select(func.count()).select_from(
        select(Article.event_id)
        .where(Article.is_published.is_(True))
        .group_by(Article.event_id)
        .having(func.count() > 1)
        .subquery()
    )
    duplicates = int((await session.execute(dup_stmt)).scalar_one())

    total_attempts = published + corrected + editorial_review
    rejection_rate = (corrected + editorial_review) / total_attempts if total_attempts else 0.0

    return AccuracySnapshot(
        generated_at=now,
        articles_published=published,
        articles_published_24h=published_24h,
        fact_validation_failures=corrected + editorial_review,
        corrections=corrected,
        source_conflicts_open=conflicts_open,
        low_confidence_publications=low_confidence,
        duplicate_publications=duplicates,
        rejection_rate=round(rejection_rate, 4),
        confidence_floor=settings.min_confidence_to_publish,
    )
