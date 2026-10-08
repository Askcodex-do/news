"""Admin/ops endpoints for ingestion observability and control (spec section 33).

Everything here is behind `require_admin`. These endpoints exist so the platform
can be operated 24/7 without shelling into a container: inspect source health,
read the queue, trigger a single source, and watch ingestion counters.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.session import get_session
from app.models.enums import EventStatus, SourceReportStatus
from app.models.event import Event, EventConflict, EventFact
from app.models.job import ProcessingJob
from app.models.source import Source, SourceHealth
from app.models.source_report import SourceReport
from app.schemas import (
    EventConflictOut,
    EventDetailOut,
    EventFactOut,
    EventIntelligenceStatsOut,
    IngestionStatsOut,
    JobOut,
    SourceHealthRow,
)
from app.services import queue
from app.services.article_generation import (
    generate_article_for_event,
    generate_localized_articles,
)
from app.services.ingestion import ingest_source
from app.services.queue import JobType
from app.services.scheduler import enqueue_due_sources
from app.services.verification import verify_event

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/sources/health", response_model=list[SourceHealthRow])
async def sources_health(
    session: AsyncSession = Depends(get_session),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[SourceHealthRow]:
    """Health for every source, worst-first so problems surface at the top."""
    stmt = (
        select(Source, SourceHealth)
        .join(SourceHealth, SourceHealth.source_id == Source.id, isouter=True)
        .order_by(SourceHealth.status.desc().nulls_first(), Source.slug)
        .limit(limit)
    )
    if status_filter:
        stmt = stmt.where(SourceHealth.status == status_filter)

    rows: list[SourceHealthRow] = []
    for source, health in (await session.execute(stmt)).all():
        rows.append(
            SourceHealthRow(
                source_slug=source.slug,
                source_name=source.name,
                enabled=source.enabled,
                is_international=source.is_international,
                status=health.status if health else "unknown",
                consecutive_failures=health.consecutive_failures if health else 0,
                success_count=health.success_count if health else 0,
                failure_count=health.failure_count if health else 0,
                avg_latency_ms=health.avg_latency_ms if health else None,
                last_success_at=source.last_success_at,
                last_failure_at=source.last_failure_at,
                last_error=health.last_error if health else None,
            )
        )
    return rows


@router.post("/sources/{slug}/poll")
async def poll_source_now(slug: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Poll one source immediately, outside the normal schedule."""
    source = (await session.execute(select(Source).where(Source.slug == slug))).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    stats = await ingest_source(session, source)
    await session.commit()
    return {"source": slug, **stats.as_dict(), "errors": stats.errors}


@router.post("/schedule/run")
async def run_scheduler(session: AsyncSession = Depends(get_session)) -> dict:
    """Enqueue poll jobs for every due source (normally done by the worker)."""
    result = await enqueue_due_sources(session)
    return {"due": result.due, "enqueued": result.enqueued, "already_queued": result.already_queued}


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    session: AsyncSession = Depends(get_session),
    job_type: JobType | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ProcessingJob]:
    stmt = select(ProcessingJob).order_by(ProcessingJob.created_at.desc()).limit(limit)
    if job_type is not None:
        stmt = stmt.where(ProcessingJob.job_type == job_type.value)
    if status_filter:
        stmt = stmt.where(ProcessingJob.status == status_filter)
    return list((await session.execute(stmt)).scalars().all())


@router.get("/ingestion/stats", response_model=IngestionStatsOut)
async def ingestion_stats(session: AsyncSession = Depends(get_session)) -> IngestionStatsOut:
    """Ingestion throughput and source health at a glance."""
    now = datetime.now(UTC)

    async def count_reports(since: datetime | None = None) -> int:
        stmt = select(func.count()).select_from(SourceReport)
        if since is not None:
            stmt = stmt.where(SourceReport.retrieved_at >= since)
        return int((await session.execute(stmt)).scalar_one())

    duplicates = int(
        (
            await session.execute(
                select(func.count())
                .select_from(SourceReport)
                .where(SourceReport.status == SourceReportStatus.DUPLICATE)
            )
        ).scalar_one()
    )

    health_counts = dict(
        (
            await session.execute(
                select(SourceHealth.status, func.count()).group_by(SourceHealth.status)
            )
        ).all()
    )

    return IngestionStatsOut(
        reports_total=await count_reports(),
        reports_last_hour=await count_reports(now - timedelta(hours=1)),
        reports_last_24h=await count_reports(now - timedelta(hours=24)),
        duplicates_total=duplicates,
        sources_healthy=int(health_counts.get("healthy", 0)),
        sources_degraded=int(health_counts.get("degraded", 0)),
        sources_down=int(health_counts.get("down", 0)),
        sources_unknown=int(health_counts.get("unknown", 0)),
        queue=await queue.queue_depth(session),
        generated_at=now,
    )


@router.get("/events", response_model=list[EventDetailOut])
async def list_events(
    session: AsyncSession = Depends(get_session),
    status_filter: EventStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[EventDetailOut]:
    """Recent events with their evidence, for operator review."""
    stmt = select(Event).order_by(Event.last_updated_at.desc()).limit(limit)
    if status_filter is not None:
        stmt = stmt.where(Event.status == status_filter)
    events = list((await session.execute(stmt)).scalars().all())

    result: list[EventDetailOut] = []
    for event in events:
        facts = (
            (
                await session.execute(
                    select(EventFact)
                    .where(EventFact.event_id == event.id)
                    .order_by(EventFact.fact_type, EventFact.fact_key)
                )
            )
            .scalars()
            .all()
        )
        conflicts = (
            (await session.execute(select(EventConflict).where(EventConflict.event_id == event.id)))
            .scalars()
            .all()
        )
        result.append(
            EventDetailOut(
                id=event.id,
                title=event.title,
                event_type=event.event_type,
                country=event.country,
                status=event.status,
                confidence_score=event.confidence_score,
                importance_score=event.importance_score,
                first_detected_at=event.first_detected_at,
                last_updated_at=event.last_updated_at,
                report_count=event.report_count,
                independent_source_count=event.independent_source_count,
                conflict_count=event.conflict_count,
                facts=[EventFactOut.model_validate(fact) for fact in facts],
                conflicts=[EventConflictOut.model_validate(conflict) for conflict in conflicts],
            )
        )
    return result


@router.get("/intelligence/stats", response_model=EventIntelligenceStatsOut)
async def intelligence_stats(
    session: AsyncSession = Depends(get_session),
) -> EventIntelligenceStatsOut:
    """Accuracy dashboard: how much is verified, conflicted, or uncertain."""
    now = datetime.now(UTC)

    async def scalar(stmt) -> float | int | None:  # type: ignore[no-untyped-def]
        return (await session.execute(stmt)).scalar_one()

    events_total = int(await scalar(select(func.count()).select_from(Event)) or 0)
    status_rows = (
        await session.execute(select(Event.status, func.count()).group_by(Event.status))
    ).all()
    events_by_status = {str(status): int(count) for status, count in status_rows}

    facts_total = int(await scalar(select(func.count()).select_from(EventFact)) or 0)
    conflicts_total = int(await scalar(select(func.count()).select_from(EventConflict)) or 0)
    conflicts_unresolved = int(
        await scalar(
            select(func.count())
            .select_from(EventConflict)
            .where(EventConflict.status == "unresolved")
        )
        or 0
    )
    events_with_conflicts = int(
        await scalar(select(func.count()).select_from(Event).where(Event.conflict_count > 0)) or 0
    )
    mean_confidence = await scalar(select(func.avg(Event.confidence_score)))
    low_confidence_published = int(
        await scalar(
            select(func.count())
            .select_from(Event)
            .where(
                Event.status.in_((EventStatus.PUBLISHED, EventStatus.UPDATING)),
                Event.confidence_score < 70,
            )
        )
        or 0
    )

    return EventIntelligenceStatsOut(
        events_total=events_total,
        events_by_status=events_by_status,
        events_verified=int(events_by_status.get(EventStatus.VERIFIED.value, 0)),
        events_unverified=int(events_by_status.get(EventStatus.UNVERIFIED.value, 0)),
        events_with_conflicts=events_with_conflicts,
        facts_total=facts_total,
        conflicts_total=conflicts_total,
        conflicts_unresolved=conflicts_unresolved,
        mean_confidence=round(float(mean_confidence), 2) if mean_confidence is not None else None,
        low_confidence_published=low_confidence_published,
        generated_at=now,
    )


@router.post("/events/{event_id}/verify")
async def verify_event_now(
    event_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    """Re-run verification for one event (facts, conflicts, confidence)."""
    try:
        event = await verify_event(session, event_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return {
        "event_id": str(event.id),
        "status": event.status.value,
        "confidence_score": event.confidence_score,
        "independent_source_count": event.independent_source_count,
        "conflict_count": event.conflict_count,
    }


@router.post("/events/{event_id}/generate")
async def generate_article_now(
    event_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    """Generate (or update) the article for one event, validating before publish.

    Requires a configured AI provider. On any validation failure the event is
    left unpublished and the reason is returned — this endpoint never publishes
    an unsupported article.
    """
    try:
        outcome = await generate_article_for_event(session, event_id, reason="manual")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return {
        "event_id": str(event_id),
        "article_id": str(outcome.article_id) if outcome.article_id else None,
        "published": outcome.published,
        "reason": outcome.reason,
        "version": outcome.version,
        "failures": outcome.validation.failures if outcome.validation else [],
    }


@router.post("/events/{event_id}/localize")
async def localize_event_now(
    event_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    """Generate country-angle editions for an event (spec section 18).

    The countries come from the ``country_sources`` mapping, never from an LLM.
    Each localized edition reuses the same verified evidence, so localization
    re-angles confirmed facts and cannot introduce local claims.
    """
    try:
        outcomes = await generate_localized_articles(session, event_id, reason="manual")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return {
        "event_id": str(event_id),
        "localized": {
            country: {
                "article_id": str(outcome.article_id) if outcome.article_id else None,
                "published": outcome.published,
                "reason": outcome.reason,
            }
            for country, outcome in outcomes.items()
        },
    }


@router.get("/articles/rejected")
async def list_rejected_articles(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    """Events in editorial review that have no published article.

    The accuracy dashboard's rejected queue: these are the stories the system
    chose not to publish, which is a feature (spec section 15), not an outage.
    """
    rows = (
        (
            await session.execute(
                select(Event)
                .where(Event.status == EventStatus.EDITORIAL_REVIEW)
                .order_by(Event.importance_score.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "event_id": str(event.id),
            "title": event.title,
            "confidence_score": event.confidence_score,
            "importance_score": event.importance_score,
            "conflict_count": event.conflict_count,
        }
        for event in rows
    ]
