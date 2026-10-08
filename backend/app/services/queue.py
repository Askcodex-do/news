"""PostgreSQL-backed job queue (spec sections 25-26).

PostgreSQL is the queue so that enqueueing work and writing its result happen in
one transaction and one datastore. Two guarantees matter:

* **Idempotency.** ``enqueue`` uses ``ON CONFLICT DO NOTHING`` on
  ``idempotency_key``; a crashed worker retrying the same logical operation can
  never create a second job.
* **Safe concurrency.** ``claim_next`` selects with ``FOR UPDATE SKIP LOCKED``,
  so multiple worker processes never hand the same job to two of them.

Redis/Celery can be layered on later for low-latency fan-out; correctness does
not depend on it.
"""

from __future__ import annotations

import json
import socket
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import JobStatus
from app.models.job import ProcessingJob

logger = get_logger(__name__)

# A job locked longer than this is assumed to belong to a dead worker.
STALE_LOCK_AFTER = timedelta(minutes=15)
# The singleton key and default lease for the single-flight scheduler.
SCHEDULER_LEASE_KEY = "scheduler"
SCHEDULER_LEASE = timedelta(minutes=5)
# Exponential backoff base; attempt N waits BASE * 2**(N-1) seconds.
RETRY_BACKOFF_BASE_SECONDS = 30
MAX_RETRY_BACKOFF_SECONDS = 3600


class JobType(StrEnum):
    """Logical units of background work.

    The value doubles as the leading segment of an idempotency key, e.g.
    ``poll_source:bbc-world:2026-10-07T19:00``.
    """

    POLL_SOURCE = "poll_source"
    # Reserved for later phases; declared here so keys stay consistent.
    DEDUP_REPORT = "dedup_report"
    CLUSTER_EVENT = "cluster_event"
    VERIFY_EVENT = "verify_event"
    GENERATE_ARTICLE = "generate_article"
    GENERATE_IMAGE = "generate_image"


@dataclass(frozen=True)
class EnqueuedJob:
    job: ProcessingJob
    created: bool


def worker_id() -> str:
    """Identify this worker process for lock bookkeeping."""
    return f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def _now() -> datetime:
    return datetime.now(UTC)


async def enqueue(
    session: AsyncSession,
    *,
    job_type: JobType,
    idempotency_key: str,
    payload: dict | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 5,
) -> EnqueuedJob:
    """Insert a job unless one with the same idempotency key already exists.

    Returns the existing (or new) row plus whether this call created it, so the
    caller can tell a fresh enqueue from a no-op duplicate.
    """
    stmt = (
        pg_insert(ProcessingJob)
        .values(
            job_type=job_type.value,
            idempotency_key=idempotency_key,
            payload=json.dumps(payload) if payload is not None else None,
            status=JobStatus.PENDING,
            run_after=run_after,
            max_attempts=max_attempts,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(ProcessingJob.id)
    )
    inserted_id = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()

    if inserted_id is not None:
        job = await session.get(ProcessingJob, inserted_id)
        assert job is not None
        return EnqueuedJob(job=job, created=True)

    existing = (
        await session.execute(
            select(ProcessingJob).where(ProcessingJob.idempotency_key == idempotency_key)
        )
    ).scalar_one()
    return EnqueuedJob(job=existing, created=False)


async def claim_next(
    session: AsyncSession,
    *,
    job_type: JobType | None = None,
    job_types: Sequence[JobType] | None = None,
    locked_by: str,
) -> ProcessingJob | None:
    """Atomically claim the oldest runnable job, or None.

    Pass ``job_type`` for a single type, or ``job_types`` for several ordered by
    preference. ``FOR UPDATE SKIP LOCKED`` lets N workers poll concurrently
    without blocking or double-processing.
    """
    if job_type is not None:
        types = [job_type]
    elif job_types:
        types = list(job_types)
    else:
        raise ValueError("claim_next requires job_type or job_types")

    stmt = select(ProcessingJob).where(
        ProcessingJob.job_type.in_([t.value for t in types]),
        ProcessingJob.status == JobStatus.PENDING,
        (ProcessingJob.run_after.is_(None)) | (ProcessingJob.run_after <= _now()),
    )
    if len(types) > 1:
        # Prefer earlier types so latency-sensitive stages (clustering,
        # verification) are not starved by a steady stream of poll jobs.
        priority = case(
            {t.value: index for index, t in enumerate(types)},
            value=ProcessingJob.job_type,
            else_=len(types),
        )
        stmt = stmt.order_by(priority)
    stmt = (
        stmt.order_by(ProcessingJob.run_after.asc().nulls_first(), ProcessingJob.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        return None

    job.status = JobStatus.RUNNING
    job.locked_at = _now()
    job.locked_by = locked_by
    job.attempts += 1
    await session.flush()
    return job


def _backoff_seconds(attempts: int) -> int:
    delay = RETRY_BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0))
    return min(delay, MAX_RETRY_BACKOFF_SECONDS)


async def mark_succeeded(session: AsyncSession, job: ProcessingJob) -> None:
    job.status = JobStatus.SUCCEEDED
    job.locked_at = None
    job.locked_by = None
    job.last_error = None
    await session.flush()


async def mark_failed(session: AsyncSession, job: ProcessingJob, error: str) -> None:
    """Retry with exponential backoff, or dead-letter once attempts are spent.

    A failed job never aborts the worker loop; the caller keeps processing other
    sources (spec section 27).
    """
    job.last_error = error[:4000]
    job.locked_at = None
    job.locked_by = None
    if job.attempts >= job.max_attempts:
        job.status = JobStatus.DEAD
    else:
        job.status = JobStatus.PENDING
        job.run_after = _now() + timedelta(seconds=_backoff_seconds(job.attempts))
    await session.flush()


async def recover_stale(session: AsyncSession) -> int:
    """Return jobs abandoned by dead workers to PENDING. Returns the count."""
    cutoff = _now() - STALE_LOCK_AFTER
    stale = (
        await session.execute(
            select(ProcessingJob).where(
                ProcessingJob.status == JobStatus.RUNNING,
                ProcessingJob.locked_at < cutoff,
            )
        )
    ).scalars()
    count = 0
    for job in stale:
        job.status = JobStatus.PENDING
        job.locked_at = None
        job.locked_by = None
        count += 1
    await session.flush()
    if count:
        logger.warning("recovered %d stale job(s)", count)
    return count


async def acquire_scheduler_lease(
    session: AsyncSession, *, worker: str, lease: timedelta = SCHEDULER_LEASE
) -> bool:
    """Try to become the single scheduling worker for this tick (spec section 25).

    The insert-or-steal is one atomic statement: it inserts the singleton row on
    first use, and otherwise takes it only when the current lease is free or
    expired. Concurrent workers contend on the primary key, so at most one wins.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.scheduler_state import SchedulerState

    now = _now()
    cutoff = now - lease
    stmt = pg_insert(SchedulerState).values(
        name=SCHEDULER_LEASE_KEY, locked_by=worker, locked_at=now
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[SchedulerState.name],
        set_={"locked_by": worker, "locked_at": now},
        where=(SchedulerState.locked_at.is_(None)) | (SchedulerState.locked_at < cutoff),
    ).returning(SchedulerState.name)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def release_scheduler_lease(
    session: AsyncSession, *, worker: str, result: str | None = None
) -> None:
    """Release the lease and record when scheduling last ran."""
    from app.models.scheduler_state import SchedulerState

    row = await session.get(SchedulerState, SCHEDULER_LEASE_KEY)
    if row is None or row.locked_by != worker:
        return
    row.locked_by = None
    row.locked_at = None
    row.last_run_at = _now()
    row.last_result = result
    await session.flush()


async def scheduler_status(session: AsyncSession) -> dict | None:
    """Observability: who last scheduled and when (spec section 33)."""
    from app.models.scheduler_state import SchedulerState

    row = await session.get(SchedulerState, SCHEDULER_LEASE_KEY)
    if row is None:
        return None
    return {
        "locked_by": row.locked_by,
        "locked_at": row.locked_at,
        "last_run_at": row.last_run_at,
        "last_result": row.last_result,
    }


async def purge_finished_jobs(session: AsyncSession, *, older_than: timedelta) -> int:
    """Delete SUCCEEDED jobs older than ``older_than``. Returns the count.

    A 24/7 queue accumulates one row per poll forever; dead-lettered jobs are
    kept for diagnosis but finished ones are not useful history.
    """
    from sqlalchemy import delete

    cutoff = _now() - older_than
    result = await session.execute(
        delete(ProcessingJob).where(
            ProcessingJob.status == JobStatus.SUCCEEDED,
            ProcessingJob.updated_at < cutoff,
        )
    )
    return int(result.rowcount or 0)


async def queue_depth(session: AsyncSession, job_type: JobType | None = None) -> dict[str, int]:
    """Observability: counts per status, optionally scoped to one job type."""
    from sqlalchemy import func

    stmt = select(ProcessingJob.status, func.count()).group_by(ProcessingJob.status)
    if job_type is not None:
        stmt = stmt.where(ProcessingJob.job_type == job_type.value)
    rows = (await session.execute(stmt)).all()
    return {str(status): count for status, count in rows}
