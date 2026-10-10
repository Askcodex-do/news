"""Phase 7 integration tests against a real PostgreSQL.

Covers the single-flight scheduler lease, the ops/accuracy aggregations and the
maintenance sweeps — all through the real code paths, no mocks.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.models.article import Article
from app.models.enums import EventStatus, JobStatus
from app.models.event import Event, EventConflict
from app.models.job import ProcessingJob
from app.models.scheduler_state import SchedulerState
from app.models.source import SourceHealth
from app.services import maintenance, observability, queue


@pytest.fixture(autouse=True)
async def clean_phase7(db_session):
    """Isolate scheduler state and any Phase 7 test fixtures."""

    async def _clean() -> None:
        await db_session.execute(delete(SchedulerState))
        await db_session.execute(delete(EventConflict))
        await db_session.commit()

    await _clean()
    yield
    await _clean()


# --- single-flight scheduler lease (spec section 25) -------------------------


async def test_first_worker_acquires_the_lease(db_session):
    assert await queue.acquire_scheduler_lease(db_session, worker="w1") is True
    await db_session.commit()
    status = await queue.scheduler_status(db_session)
    assert status["locked_by"] == "w1"


async def test_second_worker_cannot_steal_a_live_lease(db_session):
    assert await queue.acquire_scheduler_lease(db_session, worker="w1") is True
    await db_session.commit()
    # w1 holds it; w2 must lose while the lease is fresh.
    assert await queue.acquire_scheduler_lease(db_session, worker="w2") is False


async def test_expired_lease_can_be_taken_over(db_session):
    # A zero-length lease is valid only at the instant it is taken, so a second
    # worker with the same short lease can take over.
    assert await queue.acquire_scheduler_lease(db_session, worker="w1", lease=timedelta(0))
    await db_session.commit()
    assert await queue.acquire_scheduler_lease(db_session, worker="w2", lease=timedelta(0)) is True
    await db_session.commit()
    assert (await queue.scheduler_status(db_session))["locked_by"] == "w2"


async def test_release_records_last_run_and_frees_the_lease(db_session):
    await queue.acquire_scheduler_lease(db_session, worker="w1")
    await db_session.commit()
    await queue.release_scheduler_lease(db_session, worker="w1", result="due=2 enqueued=2")
    await db_session.commit()

    status = await queue.scheduler_status(db_session)
    assert status["locked_by"] is None
    assert status["last_run_at"] is not None
    assert status["last_result"] == "due=2 enqueued=2"
    # Freed, so another worker can schedule next tick.
    assert await queue.acquire_scheduler_lease(db_session, worker="w2") is True


async def test_release_by_a_non_holder_is_a_noop(db_session):
    await queue.acquire_scheduler_lease(db_session, worker="w1")
    await db_session.commit()
    await queue.release_scheduler_lease(db_session, worker="intruder", result="x")
    await db_session.commit()
    # w1 still holds it; the intruder's release did nothing.
    assert (await queue.scheduler_status(db_session))["locked_by"] == "w1"


# --- observability (spec section 33) -----------------------------------------


async def test_ops_snapshot_counts_sources_and_scheduler(db_session, clean_events):
    await queue.acquire_scheduler_lease(db_session, worker="w1")
    await db_session.commit()
    snapshot = await observability.ops_snapshot(db_session)
    assert snapshot.sources.total >= 1
    assert snapshot.scheduler is not None
    assert snapshot.jobs.queue is not None


async def test_accuracy_snapshot_counts_corrections_and_conflicts(
    db_session, clean_events, clean_articles, make_source, make_report
):
    now = datetime.now(UTC)
    source = await make_source(name="Reuters")
    await make_report(source, title="Quake hits Japan")
    event = Event(
        title="Quake hits Japan",
        event_type="earthquake",
        status=EventStatus.PUBLISHED,
        first_detected_at=now,
        last_updated_at=now,
        confidence_score=90.0,
        importance_score=70.0,
    )
    db_session.add(event)
    await db_session.flush()
    db_session.add(
        EventConflict(
            event_id=event.id,
            fact_type="casualties",
            claim="deaths differ",
            status="unresolved",
            detected_at=now,
        )
    )
    article = Article(
        event_id=event.id,
        is_global=True,
        slug=f"p7-{uuid.uuid4().hex[:8]}",
        headline="Quake hits Japan",
        body="body",
        is_published=True,
        published_at=now,
        confidence_score=90.0,
        importance_score=70.0,
    )
    db_session.add(article)
    await db_session.flush()

    snap = await observability.accuracy_snapshot(db_session)
    assert snap.source_conflicts_open >= 1
    assert snap.articles_published >= 1
    assert snap.confidence_floor == 70.0


# --- maintenance (spec sections 22, 25) --------------------------------------


async def test_archive_stale_events_only_archives_quiet_active_events(
    db_session, clean_events, make_source, make_report
):
    now = datetime.now(UTC)
    source = await make_source(name="Reuters")
    await make_report(source, title="Quiet event")

    old = Event(
        title="Old quiet event",
        event_type="other",
        status=EventStatus.PUBLISHED,
        first_detected_at=now - timedelta(days=30),
        last_updated_at=now - timedelta(days=20),
    )
    fresh = Event(
        title="Fresh event",
        event_type="other",
        status=EventStatus.PUBLISHED,
        first_detected_at=now,
        last_updated_at=now,
    )
    already = Event(
        title="Already archived",
        event_type="other",
        status=EventStatus.ARCHIVED,
        first_detected_at=now - timedelta(days=40),
        last_updated_at=now - timedelta(days=40),
    )
    db_session.add_all([old, fresh, already])
    await db_session.flush()

    archived = await maintenance.archive_stale_events(db_session, now=now, older_than_hours=168)
    assert archived == 1

    assert (await db_session.get(Event, old.id)).status == EventStatus.ARCHIVED
    assert (await db_session.get(Event, fresh.id)).status == EventStatus.PUBLISHED
    assert (await db_session.get(Event, already.id)).status == EventStatus.ARCHIVED


async def test_prune_finished_jobs_keeps_dead_and_recent(db_session, clean_phase7_jobs):
    now = datetime.now(UTC)
    old_done = ProcessingJob(
        job_type="poll_source",
        idempotency_key=f"p7-old-{uuid.uuid4().hex}",
        status=JobStatus.SUCCEEDED,
        attempts=1,
    )
    old_dead = ProcessingJob(
        job_type="poll_source",
        idempotency_key=f"p7-dead-{uuid.uuid4().hex}",
        status=JobStatus.DEAD,
        attempts=5,
    )
    fresh_done = ProcessingJob(
        job_type="poll_source",
        idempotency_key=f"p7-fresh-{uuid.uuid4().hex}",
        status=JobStatus.SUCCEEDED,
        attempts=1,
    )
    db_session.add_all([old_done, old_dead, fresh_done])
    await db_session.flush()
    # Age the two old rows past retention.
    await db_session.execute(
        ProcessingJob.__table__.update()
        .where(ProcessingJob.id.in_([old_done.id, old_dead.id]))
        .values(updated_at=now - timedelta(days=30))
    )
    await db_session.flush()

    pruned = await maintenance.prune_finished_jobs(db_session, now=now, retention_days=7)
    assert pruned == 1

    remaining = set(
        (
            await db_session.execute(
                select(ProcessingJob.id).where(
                    ProcessingJob.id.in_([old_done.id, old_dead.id, fresh_done.id])
                )
            )
        )
        .scalars()
        .all()
    )
    assert old_done.id not in remaining  # finished + old -> pruned
    assert old_dead.id in remaining  # dead is kept for diagnosis
    assert fresh_done.id in remaining  # finished but recent


@pytest.fixture
async def clean_phase7_jobs(db_session):
    async def _clean() -> None:
        await db_session.execute(
            delete(ProcessingJob).where(ProcessingJob.job_type == "poll_source")
        )
        await db_session.commit()

    await _clean()
    yield
    await _clean()


# --- source health surface ---------------------------------------------------


async def test_ops_snapshot_reports_down_sources(db_session, clean_events, make_source):
    source = await make_source(name="Down Feed")
    db_session.add(SourceHealth(source_id=source.id, status="down", consecutive_failures=5))
    await db_session.flush()
    snap = await observability.ops_snapshot(db_session)
    assert snap.sources.down >= 1
    assert "Down Feed" in snap.sources.offline
