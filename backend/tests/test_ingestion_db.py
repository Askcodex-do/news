"""Ingestion, deduplication and job-queue tests against a real PostgreSQL.

No mocks: these drive the actual pipeline, queue SQL (including
``FOR UPDATE SKIP LOCKED`` and ``ON CONFLICT``) and health bookkeeping. They run
against the isolated test database configured in conftest.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.models.enums import JobStatus, SourceReportStatus, SourceType
from app.models.job import ProcessingJob
from app.models.source import Source, SourceHealth
from app.models.source_report import SourceReport
from app.services import queue, scheduler, source_health
from app.services.feed_parser import ParsedEntry
from app.services.ingestion import ingest_entries
from app.services.queue import JobType


@pytest.fixture
async def clean_queue(db_session):
    """Empty the job table so queue tests never see other tests' jobs.

    The queue is global (``claim_next`` claims any pending job), so without this
    a job enqueued by one test — or left behind by a failed run — would be picked
    up by the next. Runs before and after each test.
    """
    await db_session.execute(delete(ProcessingJob))
    await db_session.commit()
    yield
    await db_session.execute(delete(ProcessingJob))
    await db_session.commit()


@pytest.fixture
async def source(db_session):
    """A throwaway source, removed afterwards so the suite is repeatable."""
    src = Source(
        slug=f"test-{uuid.uuid4().hex[:8]}",
        name="Test Feed",
        type=SourceType.rss,
        website_url="https://example.com",
        rss_url="https://example.com/rss.xml",
        language="en",
        poll_interval_seconds=300,
    )
    db_session.add(src)
    await db_session.flush()
    db_session.add(SourceHealth(source_id=src.id, status="unknown"))
    await db_session.flush()
    yield src
    await db_session.execute(delete(SourceReport).where(SourceReport.source_id == src.id))
    await db_session.execute(delete(SourceHealth).where(SourceHealth.source_id == src.id))
    await db_session.execute(delete(Source).where(Source.id == src.id))
    await db_session.commit()


def _entry(title: str, link: str, description: str = "") -> ParsedEntry:
    from app.services.textnorm import canonicalize_url

    return ParsedEntry(
        title=title,
        description=description or title,
        link=link,
        canonical_url=canonicalize_url(link),
        published_at=datetime.now(UTC),
        author=None,
        language="en",
    )


async def test_ingest_stores_new_reports(db_session, source):
    stats = await ingest_entries(
        db_session,
        source,
        [
            _entry("Earthquake strikes Japan", "https://example.com/a"),
            _entry("Flooding in Bangladesh", "https://example.com/b"),
        ],
    )
    assert stats.stored == 2
    assert stats.rejected == 0

    rows = (
        (await db_session.execute(select(SourceReport).where(SourceReport.source_id == source.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert all(r.status == SourceReportStatus.NEW for r in rows)
    assert all(r.simhash for r in rows)


async def test_level1_url_duplicate_is_not_stored_twice(db_session, source):
    link = "https://example.com/story?id=1"
    first = await ingest_entries(db_session, source, [_entry("Original headline", link)])
    assert first.stored == 1

    # Same story URL with a tracking parameter: canonical form is identical.
    second = await ingest_entries(
        db_session, source, [_entry("Original headline", link + "&utm_source=newsletter")]
    )
    assert second.stored == 0
    assert second.duplicate_url == 1


async def test_level1_duplicate_inside_one_batch_is_collapsed(db_session, source):
    link = "https://example.com/same"
    batch = [_entry("Headline", link), _entry("Headline", link)]
    stats = await ingest_entries(db_session, source, batch)
    assert stats.stored == 1
    assert stats.duplicate_batch == 1


async def test_level2_identical_content_from_different_urls_is_duplicate(db_session, source):
    body = "Twelve people were killed when an earthquake struck Japan."
    first = await ingest_entries(
        db_session, source, [_entry("Earthquake strikes Japan", "https://example.com/x", body)]
    )
    assert first.stored == 1

    # Different URL, identical normalized content -> Level 2 duplicate.
    second = await ingest_entries(
        db_session, source, [_entry("Earthquake strikes Japan", "https://example.com/y", body)]
    )
    assert second.stored == 0
    assert second.duplicate_content == 1


async def test_rejected_entries_are_reported_not_stored(db_session, source):
    stats = await ingest_entries(db_session, source, [_entry("", "https://example.com/empty")])
    assert stats.stored == 0
    assert stats.rejected == 1
    assert stats.errors


async def test_health_success_clears_failure_streak(db_session, source):
    await source_health.record_failure(db_session, source, error="boom", latency_ms=100)
    await source_health.record_failure(db_session, source, error="boom", latency_ms=100)
    health = await source_health.snapshot(db_session, source.id)
    assert health is not None
    assert health.consecutive_failures == 2
    assert health.status == "degraded"

    await source_health.record_success(db_session, source, latency_ms=50)
    health = await source_health.snapshot(db_session, source.id)
    assert health.consecutive_failures == 0
    assert health.status == "healthy"
    assert health.last_error is None


async def test_health_escalates_to_down_after_repeated_failures(db_session, source):
    for _ in range(source_health.DOWN_AFTER):
        await source_health.record_failure(db_session, source, error="unreachable")
    health = await source_health.snapshot(db_session, source.id)
    assert health.status == "down"
    assert source.last_failure_at is not None


async def test_enqueue_is_idempotent_on_key(db_session, clean_queue):
    key = f"poll_source:test:{uuid.uuid4().hex}"
    first = await queue.enqueue(db_session, job_type=JobType.POLL_SOURCE, idempotency_key=key)
    second = await queue.enqueue(db_session, job_type=JobType.POLL_SOURCE, idempotency_key=key)
    assert first.created is True
    assert second.created is False
    assert first.job.id == second.job.id

    count = (
        (
            await db_session.execute(
                select(ProcessingJob).where(ProcessingJob.idempotency_key == key)
            )
        )
        .scalars()
        .all()
    )
    assert len(count) == 1
    await db_session.execute(delete(ProcessingJob).where(ProcessingJob.idempotency_key == key))
    await db_session.commit()


async def test_claim_next_marks_job_running_and_is_exclusive(db_session, clean_queue):
    key = f"poll_source:test:{uuid.uuid4().hex}"
    await queue.enqueue(db_session, job_type=JobType.POLL_SOURCE, idempotency_key=key)
    await db_session.commit()

    claimed = await queue.claim_next(db_session, job_type=JobType.POLL_SOURCE, locked_by="w1")
    assert claimed is not None
    assert claimed.status == JobStatus.RUNNING
    assert claimed.locked_by == "w1"
    assert claimed.attempts == 1
    await db_session.commit()

    # A second worker must not be handed the same job.
    again = await queue.claim_next(db_session, job_type=JobType.POLL_SOURCE, locked_by="w2")
    assert again is None

    await db_session.execute(delete(ProcessingJob).where(ProcessingJob.idempotency_key == key))
    await db_session.commit()


async def test_claim_next_prefers_earlier_job_types(db_session, clean_queue):
    """A pending poll must not starve a pending clustering job.

    The worker claims several job types at once; without a priority order the
    oldest job wins and a steady stream of polls starves clustering.
    """
    poll_key = f"poll_source:test:{uuid.uuid4().hex}"
    cluster_key = f"cluster_event:test:{uuid.uuid4().hex}"
    await queue.enqueue(db_session, job_type=JobType.POLL_SOURCE, idempotency_key=poll_key)
    await db_session.commit()
    await queue.enqueue(db_session, job_type=JobType.CLUSTER_EVENT, idempotency_key=cluster_key)
    await db_session.commit()

    claimed = await queue.claim_next(
        db_session,
        job_types=(JobType.CLUSTER_EVENT, JobType.POLL_SOURCE),
        locked_by="w1",
    )
    assert claimed is not None
    assert claimed.job_type == JobType.CLUSTER_EVENT.value

    await db_session.execute(
        delete(ProcessingJob).where(ProcessingJob.idempotency_key.in_([poll_key, cluster_key]))
    )
    await db_session.commit()


async def test_failed_job_is_retried_with_backoff_then_dead_lettered(db_session, clean_queue):
    key = f"poll_source:test:{uuid.uuid4().hex}"
    await queue.enqueue(
        db_session, job_type=JobType.POLL_SOURCE, idempotency_key=key, max_attempts=2
    )
    await db_session.commit()

    job = await queue.claim_next(db_session, job_type=JobType.POLL_SOURCE, locked_by="w1")
    await queue.mark_failed(db_session, job, "transient")
    assert job.status == JobStatus.PENDING  # still has an attempt left
    assert job.run_after is not None
    await db_session.commit()

    # Skip the backoff wait: the job becomes due again once its window passes.
    job.run_after = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    job = await queue.claim_next(db_session, job_type=JobType.POLL_SOURCE, locked_by="w1")
    assert job is not None
    await queue.mark_failed(db_session, job, "again")
    assert job.status == JobStatus.DEAD  # attempts exhausted
    await db_session.commit()

    await db_session.execute(delete(ProcessingJob).where(ProcessingJob.idempotency_key == key))
    await db_session.commit()


async def test_recover_stale_returns_abandoned_jobs_to_pending(db_session, clean_queue):
    key = f"poll_source:test:{uuid.uuid4().hex}"
    await queue.enqueue(db_session, job_type=JobType.POLL_SOURCE, idempotency_key=key)
    await db_session.commit()

    job = await queue.claim_next(db_session, job_type=JobType.POLL_SOURCE, locked_by="dead")
    # Simulate a worker that died holding the lock.
    job.locked_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.commit()

    recovered = await queue.recover_stale(db_session)
    assert recovered >= 1
    refreshed = await db_session.get(ProcessingJob, job.id)
    assert refreshed.status == JobStatus.PENDING
    assert refreshed.locked_at is None
    await db_session.commit()

    await db_session.execute(delete(ProcessingJob).where(ProcessingJob.idempotency_key == key))
    await db_session.commit()


async def test_enqueue_due_sources_is_idempotent_within_a_window(db_session, source, clean_queue):
    now = datetime.now(UTC)
    # Make the source due and pin the window so both calls share a bucket.
    source.last_success_at = now - timedelta(hours=1)
    await db_session.commit()

    first = await scheduler.enqueue_due_sources(db_session, now=now)
    second = await scheduler.enqueue_due_sources(db_session, now=now)
    assert first.enqueued >= 1
    assert second.enqueued == 0  # same window => no new jobs

    keys = [
        f"{JobType.POLL_SOURCE.value}:{source.slug}:"
        f"{scheduler._bucket(now, source.poll_interval_seconds)}"
    ]
    await db_session.execute(delete(ProcessingJob).where(ProcessingJob.idempotency_key.in_(keys)))
    await db_session.commit()


async def test_process_pending_jobs_isolates_a_failing_source(db_session, clean_queue):
    """A job whose source vanished fails without stopping the loop."""
    key = f"poll_source:test:{uuid.uuid4().hex}"
    await queue.enqueue(
        db_session,
        job_type=JobType.POLL_SOURCE,
        idempotency_key=key,
        payload={"source_id": str(uuid.uuid4()), "slug": "ghost"},
    )
    await db_session.commit()

    ran = await scheduler.process_pending_jobs(db_session, worker="w1", max_jobs=5)
    assert ran >= 1
    job = (
        await db_session.execute(select(ProcessingJob).where(ProcessingJob.idempotency_key == key))
    ).scalar_one()
    assert job.status in {JobStatus.PENDING, JobStatus.DEAD}
    assert job.last_error

    await db_session.execute(delete(ProcessingJob).where(ProcessingJob.idempotency_key == key))
    await db_session.commit()


async def test_pipeline_survives_unreachable_source(db_session, source):
    """ingest_source records failure and returns stats instead of raising."""
    from app.services.ingestion import ingest_source

    source.rss_url = "https://127.0.0.1:1/does-not-exist.xml"
    await db_session.flush()

    stats = await ingest_source(db_session, source)
    assert stats.stored == 0
    assert stats.errors

    health = await source_health.snapshot(db_session, source.id)
    assert health is not None
    assert health.consecutive_failures == 1
