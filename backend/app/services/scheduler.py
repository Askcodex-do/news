"""Scheduling and job dispatch for continuous ingestion (spec sections 25-27).

The scheduler is split into two independent steps so either can run in its own
process:

1. ``enqueue_due_sources`` decides which sources are due and enqueues a
   ``poll_source`` job for each. Idempotency keys make repeated calls safe.
2. ``process_pending_jobs`` claims runnable jobs and executes them, isolating
   failures so one bad source cannot stall the loop.

Neither step holds a long transaction: scheduling and execution touch the
database briefly and release the connection.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import EventStatus
from app.models.job import ProcessingJob
from app.models.source import Source
from app.services import queue
from app.services.article_generation import generate_article_for_event
from app.services.clustering import cluster_report
from app.services.ingestion import IngestStats, ingest_source
from app.services.queue import JobType
from app.services.verification import verify_event

logger = get_logger(__name__)

# A source that keeps failing is polled less often, up to this multiple of its
# configured interval, so a dead feed does not monopolise the worker.
MAX_BACKOFF_MULTIPLIER = 8


@dataclass
class ScheduleResult:
    due: int = 0
    enqueued: int = 0
    already_queued: int = 0


# Job types the worker loop executes, in priority order. Clustering and
# verification are latency-sensitive (they gate publishing) and are produced by
# polling, so they run ahead of the next poll.
_HANDLED_JOB_TYPES = (
    JobType.CLUSTER_EVENT,
    JobType.VERIFY_EVENT,
    JobType.GENERATE_ARTICLE,
    JobType.POLL_SOURCE,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _bucket(dt: datetime, interval_seconds: int) -> str:
    """Collapse a timestamp into the polling window it belongs to.

    Using the window (not the exact instant) in the idempotency key means two
    scheduler passes in the same window produce one job, not two.
    """
    epoch = int(dt.timestamp())
    return str(epoch - (epoch % max(interval_seconds, 1)))


def _poll_key(source: Source, when: datetime) -> str:
    bucket = _bucket(when, source.poll_interval_seconds)
    return f"{JobType.POLL_SOURCE.value}:{source.slug}:{bucket}"


def _next_due(source: Source, *, now: datetime, failures: int) -> datetime:
    # Healthy sources poll on their configured interval; failing sources back off.
    multiplier = min(max(failures, 1), MAX_BACKOFF_MULTIPLIER) if failures > 0 else 1
    interval = source.poll_interval_seconds * multiplier
    last = source.last_success_at or source.last_failure_at
    if last is None:
        return now
    return last + timedelta(seconds=interval)


async def enqueue_due_sources(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 200
) -> ScheduleResult:
    """Enqueue ``poll_source`` jobs for every enabled source that is due."""
    now = now or _now()
    result = ScheduleResult()

    sources = (await session.execute(select(Source).where(Source.enabled.is_(True)))).scalars()
    checked = 0
    for source in sources:
        if checked >= limit:
            break
        checked += 1

        failures = source.health.consecutive_failures if source.health else 0
        if _next_due(source, now=now, failures=failures) > now:
            continue

        result.due += 1
        enqueued = await queue.enqueue(
            session,
            job_type=JobType.POLL_SOURCE,
            idempotency_key=_poll_key(source, now),
            payload={"source_id": str(source.id), "slug": source.slug},
        )
        if enqueued.created:
            result.enqueued += 1
        else:
            result.already_queued += 1

    await session.commit()
    if result.enqueued:
        logger.info(
            "scheduled %d poll job(s) (%d already queued, %d due)",
            result.enqueued,
            result.already_queued,
            result.due,
        )
    return result


async def _run_poll_job(session: AsyncSession, job_payload: str | None) -> IngestStats:
    payload = json.loads(job_payload) if job_payload else {}
    source_id = uuid.UUID(payload["source_id"])
    source = await session.get(Source, source_id)
    if source is None:
        raise ValueError(f"source {source_id} no longer exists")
    if not source.enabled:
        return IngestStats(errors=["source disabled"])
    stats = await ingest_source(session, source)
    # Hand each new report to the clustering stage. Idempotent by report id, so
    # a retried poll never double-clusters a report.
    for report_id in stats.stored_ids:
        await queue.enqueue(
            session,
            job_type=JobType.CLUSTER_EVENT,
            idempotency_key=f"{JobType.CLUSTER_EVENT.value}:{report_id}",
            payload={"report_id": str(report_id)},
        )
    return stats


async def _run_cluster_job(session: AsyncSession, job_payload: str | None) -> str:
    payload = json.loads(job_payload) if job_payload else {}
    report_id = uuid.UUID(payload["report_id"])
    event, created = await cluster_report(session, report_id)
    # Re-verify the event now that it has a new member. The key includes the
    # event's report count so each distinct membership state verifies once.
    await queue.enqueue(
        session,
        job_type=JobType.VERIFY_EVENT,
        idempotency_key=(
            f"{JobType.VERIFY_EVENT.value}:{event.id}:{event.report_count}:{int(created)}"
        ),
        payload={"event_id": str(event.id)},
    )
    return f"{'created' if created else 'attached'} event {event.id}"


async def _run_verify_job(session: AsyncSession, job_payload: str | None) -> str:
    payload = json.loads(job_payload) if job_payload else {}
    event_id = uuid.UUID(payload["event_id"])
    event = await verify_event(session, event_id)
    # A verified event, or an already-published one that just changed, gets an
    # article (spec section 22: one article, updated). Keyed by report count so
    # each distinct membership state generates once and retries are safe.
    if event.status in (EventStatus.VERIFIED, EventStatus.PUBLISHED, EventStatus.UPDATING):
        await queue.enqueue(
            session,
            job_type=JobType.GENERATE_ARTICLE,
            idempotency_key=(f"{JobType.GENERATE_ARTICLE.value}:{event.id}:r{event.report_count}"),
            payload={"event_id": str(event.id)},
        )
    return f"event {event.id} status={event.status} confidence={event.confidence_score:.1f}"


async def _run_generate_job(session: AsyncSession, job_payload: str | None) -> str:
    payload = json.loads(job_payload) if job_payload else {}
    event_id = uuid.UUID(payload["event_id"])
    outcome = await generate_article_for_event(session, event_id)
    return f"event {event_id} published={outcome.published} ({outcome.reason})"


async def process_pending_jobs(session: AsyncSession, *, worker: str, max_jobs: int = 25) -> int:
    """Claim and run up to ``max_jobs`` pending jobs. Returns how many ran.

    Handlers are dispatched by job type; each runs in its own commit so one
    failure cannot roll back another job's work.
    """
    handled = 0
    for _ in range(max_jobs):
        job = await _claim_any(session, worker)
        if job is None:
            break
        job_id = job.id
        job_type = job.job_type
        # Commit the claim so other workers see the lock immediately.
        await session.commit()

        try:
            if job_type == JobType.POLL_SOURCE.value:
                stats = await _run_poll_job(session, job.payload)
                await queue.mark_succeeded(session, job)
                await session.commit()
                logger.debug("job %s done: %s", job_id, stats.as_dict())
            elif job_type == JobType.CLUSTER_EVENT.value:
                summary = await _run_cluster_job(session, job.payload)
                await queue.mark_succeeded(session, job)
                await session.commit()
                logger.debug("job %s done: %s", job_id, summary)
            elif job_type == JobType.VERIFY_EVENT.value:
                summary = await _run_verify_job(session, job.payload)
                await queue.mark_succeeded(session, job)
                await session.commit()
                logger.debug("job %s done: %s", job_id, summary)
            elif job_type == JobType.GENERATE_ARTICLE.value:
                summary = await _run_generate_job(session, job.payload)
                await queue.mark_succeeded(session, job)
                await session.commit()
                logger.debug("job %s done: %s", job_id, summary)
            else:
                raise ValueError(f"no handler for job type {job_type!r}")
        except Exception as exc:  # noqa: BLE001 - isolate per-job failures
            await session.rollback()
            failed = await session.get(ProcessingJob, job_id)
            if failed is not None:
                await queue.mark_failed(session, failed, f"{type(exc).__name__}: {exc}")
                await session.commit()
            logger.exception("job %s failed", job_id)
        handled += 1
    return handled


async def _claim_any(session: AsyncSession, worker: str) -> ProcessingJob | None:
    """Claim the oldest runnable job across every handled job type.

    A single priority-ordered claim, not one claim per type: claiming per type
    would always return a poll job when one is pending and starve the
    clustering and verification stages behind a steady stream of polls.
    """
    return await queue.claim_next(session, job_types=_HANDLED_JOB_TYPES, locked_by=worker)
