"""Admin/ops endpoints for ingestion observability and control (spec section 33).

Everything here is behind `require_admin`. These endpoints exist so the platform
can be operated 24/7 without shelling into a container: inspect source health,
read the queue, trigger a single source, and watch ingestion counters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.session import get_session
from app.models.enums import SourceReportStatus
from app.models.job import ProcessingJob
from app.models.source import Source, SourceHealth
from app.models.source_report import SourceReport
from app.schemas import IngestionStatsOut, JobOut, SourceHealthRow
from app.services import queue
from app.services.ingestion import ingest_source
from app.services.queue import JobType
from app.services.scheduler import enqueue_due_sources

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
