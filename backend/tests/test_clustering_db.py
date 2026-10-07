"""Clustering and verification tests against a real PostgreSQL.

No mocks: these drive the actual clustering and verification code paths, the
event/fact/conflict tables, and the queue hand-off between stages. They run
against the isolated test database configured in conftest.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.models.enums import EventStatus, SourceReportStatus, SourceType
from app.models.event import (
    Event,
    EventConflict,
    EventFact,
    EventFactSource,
    EventReport,
    EventUpdate,
)
from app.models.job import ProcessingJob
from app.models.source import Source
from app.models.source_report import SourceReport
from app.services import queue, scheduler
from app.services.clustering import cluster_report
from app.services.queue import JobType
from app.services.verification import verify_event


@pytest.fixture
async def clean_events(db_session, make_source):
    """Remove events and their dependents before and after each test.

    Depends on ``make_source`` so that this teardown (which deletes events)
    runs before ``make_source`` teardown deletes the reports those events point
    at — otherwise the report delete violates the ``event_reports`` FK.
    """

    async def _clean() -> None:
        await db_session.execute(delete(EventUpdate))
        await db_session.execute(delete(EventConflict))
        await db_session.execute(delete(EventReport))
        await db_session.execute(delete(EventFactSource))
        await db_session.execute(delete(EventFact))
        await db_session.execute(delete(Event))
        await db_session.commit()

    await _clean()
    yield
    await _clean()


@pytest.fixture
async def make_source(db_session):
    """Factory for throwaway sources with optional lineage and reliability."""
    created: list[uuid.UUID] = []

    async def _make(
        *,
        name: str = "Test Feed",
        country: str | None = None,
        lineage_root_id: uuid.UUID | None = None,
        reliability_score: float = 80.0,
    ) -> Source:
        src = Source(
            slug=f"evt-{uuid.uuid4().hex[:8]}",
            name=name,
            country=country,
            type=SourceType.rss,
            website_url="https://example.com",
            rss_url="https://example.com/rss.xml",
            language="en",
            poll_interval_seconds=300,
            lineage_root_id=lineage_root_id,
            reliability_score=reliability_score,
        )
        db_session.add(src)
        await db_session.flush()
        created.append(src.id)
        return src

    yield _make

    for source_id in created:
        await db_session.execute(delete(SourceReport).where(SourceReport.source_id == source_id))
    # Delete dependents before their lineage roots (a republisher points at a
    # wire source), so reverse creation order avoids the self-referential FK.
    for source_id in reversed(created):
        await db_session.execute(delete(Source).where(Source.id == source_id))
    await db_session.commit()


@pytest.fixture
async def make_report(db_session):
    """Factory for source reports belonging to a source."""

    async def _make(
        source: Source,
        *,
        title: str,
        description: str = "",
        published_at: datetime | None = None,
    ) -> SourceReport:
        now = datetime.now(UTC)
        report = SourceReport(
            source_id=source.id,
            source_url=f"https://example.com/{uuid.uuid4().hex}",
            canonical_url=f"https://example.com/{uuid.uuid4().hex}",
            canonical_url_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            title=title,
            description=description or None,
            published_at=published_at or now,
            retrieved_at=now,
            content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            status=SourceReportStatus.NEW,
        )
        db_session.add(report)
        await db_session.flush()
        return report

    return _make


async def _events_for_report(db_session, report_id: uuid.UUID) -> list[Event]:
    return list(
        (
            await db_session.execute(
                select(Event)
                .join(EventReport, EventReport.event_id == Event.id)
                .where(EventReport.source_report_id == report_id)
            )
        )
        .scalars()
        .all()
    )


# --- Clustering ---------------------------------------------------------------


async def test_identical_topic_reports_cluster_into_one_event(
    db_session, clean_events, make_source, make_report
):
    source = await make_source()
    now = datetime.now(UTC)
    a = await make_report(
        source,
        title="Earthquake strikes Japan killing 12",
        description="A magnitude 6.8 earthquake hit Japan.",
        published_at=now,
    )
    b = await make_report(
        source,
        title="Earthquake strikes Japan killing 12",
        description="A magnitude 6.8 earthquake hit Japan.",
        published_at=now + timedelta(minutes=5),
    )

    event_a, created_a = await cluster_report(db_session, a.id)
    event_b, created_b = await cluster_report(db_session, b.id)

    assert created_a is True
    assert created_b is False
    assert event_a.id == event_b.id

    members = (
        (await db_session.execute(select(EventReport).where(EventReport.event_id == event_a.id)))
        .scalars()
        .all()
    )
    assert len(members) == 2


async def test_unrelated_reports_create_separate_events(
    db_session, clean_events, make_source, make_report
):
    source = await make_source()
    a = await make_report(source, title="Earthquake strikes Japan killing 12")
    b = await make_report(source, title="Parliament passes the annual budget")

    event_a, created_a = await cluster_report(db_session, a.id)
    event_b, created_b = await cluster_report(db_session, b.id)

    assert created_a and created_b
    assert event_a.id != event_b.id


async def test_different_events_stay_apart_despite_shared_type(
    db_session, clean_events, make_source, make_report
):
    source = await make_source()
    a = await make_report(source, title="Earthquake strikes Japan killing 12")
    b = await make_report(source, title="Election earthquake of support for the party")

    event_a, _ = await cluster_report(db_session, a.id)
    event_b, _ = await cluster_report(db_session, b.id)

    # Both classify as "earthquake", so the type labels agree; semantic
    # distance alone must keep these unrelated stories apart.
    assert event_a.id != event_b.id


async def test_identical_headline_merges_across_type_labels(
    db_session, clean_events, make_source, make_report
):
    source = await make_source()
    title = "Ten injured in knife attack at Polish school"
    a = await make_report(source, title=title, description="Police said the suspect was a student.")
    b = await make_report(source, title=title, description="WARSAW: Ten people were injured.")

    # Keyword classifiers can label the same headline differently; an identical
    # headline must still merge because a type mismatch is only a penalty.
    event_a, created_a = await cluster_report(db_session, a.id)
    event_b, created_b = await cluster_report(db_session, b.id)

    assert created_a is True
    assert created_b is False
    assert event_a.id == event_b.id


async def test_nearest_candidate_survives_candidate_cap(
    db_session, clean_events, make_source, make_report, monkeypatch
):
    """Regression: a burst of newer events must not hide the matching event.

    Candidates are capped, so ordering by recency used to push the real match
    out of the window during a burst and split one event into several. Ordering
    by embedding distance keeps the match in the cap.
    """
    from app.services import clustering

    monkeypatch.setattr(clustering, "MAX_CANDIDATES", 2)
    source = await make_source()
    now = datetime.now(UTC)

    first = await make_report(
        source,
        title="Earthquake strikes Japan killing 12",
        description="A magnitude 6.8 earthquake hit Japan.",
        published_at=now,
    )
    event, _ = await cluster_report(db_session, first.id)

    # Newer, unrelated events that a recency-ordered cap would prefer.
    for offset, title in enumerate(
        ["Parliament passes the annual budget", "Wildfire forces evacuations in Greece"]
    ):
        decoy = await make_report(
            source, title=title, published_at=now + timedelta(minutes=offset + 1)
        )
        await cluster_report(db_session, decoy.id)

    duplicate = await make_report(
        source,
        title="Earthquake strikes Japan killing 12",
        description="A magnitude 6.8 earthquake hit Japan.",
        published_at=now + timedelta(minutes=1),
    )
    matched, created = await cluster_report(db_session, duplicate.id)

    assert created is False
    assert matched.id == event.id


async def test_report_status_becomes_processed_after_clustering(
    db_session, clean_events, make_source, make_report
):
    source = await make_source()
    report = await make_report(source, title="Flooding displaces thousands in Bangladesh")
    await cluster_report(db_session, report.id)
    refreshed = await db_session.get(SourceReport, report.id)
    assert refreshed.status == SourceReportStatus.PROCESSED
    assert refreshed.processed_at is not None
    assert refreshed.event_type == "flood"


async def test_clustering_records_a_timeline_update(
    db_session, clean_events, make_source, make_report
):
    source = await make_source()
    report = await make_report(source, title="Wildfire forces evacuations in Greece")
    event, _ = await cluster_report(db_session, report.id)
    updates = (
        (await db_session.execute(select(EventUpdate).where(EventUpdate.event_id == event.id)))
        .scalars()
        .all()
    )
    assert any(update.update_type == "detected" for update in updates)


# --- Verification: facts and confidence ---------------------------------------


async def test_verification_builds_facts_and_confidence(
    db_session, clean_events, make_source, make_report
):
    source = await make_source(name="Reuters", reliability_score=95.0)
    report = await make_report(
        source,
        title="Magnitude 6.8 earthquake strikes Japan, killing 12",
        description="Officials confirmed 12 deaths after the quake.",
    )
    event, _ = await cluster_report(db_session, report.id)
    event = await verify_event(db_session, event.id)

    facts = (
        (await db_session.execute(select(EventFact).where(EventFact.event_id == event.id)))
        .scalars()
        .all()
    )
    keys = {fact.fact_key for fact in facts}
    assert "number:deaths:12" in keys
    assert "number:magnitude:6.8" in keys
    assert event.report_count == 1
    assert event.independent_source_count == 1
    assert 0 < event.confidence_score <= 99
    # A single source must not reach the publication floor.
    assert event.status in (EventStatus.UNVERIFIED, EventStatus.VERIFYING)


async def test_lineage_reduces_independent_source_count(
    db_session, clean_events, make_source, make_report
):
    wire = await make_source(name="Reuters")
    # Two republishers sharing the wire's lineage.
    republisher_a = await make_source(name="Site A", lineage_root_id=wire.id)
    republisher_b = await make_source(name="Site B", lineage_root_id=wire.id)

    reports = [
        await make_report(wire, title="Earthquake strikes Japan killing 12"),
        await make_report(republisher_a, title="Earthquake strikes Japan killing 12"),
        await make_report(republisher_b, title="Earthquake strikes Japan killing 12"),
    ]
    for report in reports:
        await cluster_report(db_session, report.id)

    event = (await _events_for_report(db_session, reports[0].id))[0]
    event = await verify_event(db_session, event.id)

    assert event.report_count == 3
    # All three trace back to one wire chain.
    assert event.independent_source_count == 1


async def test_conflicting_numbers_create_a_conflict_and_block_publish(
    db_session, clean_events, make_source, make_report
):
    source_a = await make_source(name="Site A")
    source_b = await make_source(name="Site B")

    reports = [
        await make_report(source_a, title="Explosion kills 5 in market"),
        await make_report(source_b, title="Explosion kills 12 in market"),
    ]
    for report in reports:
        await cluster_report(db_session, report.id)

    event = (await _events_for_report(db_session, reports[0].id))[0]
    event = await verify_event(db_session, event.id)

    conflicts = (
        (await db_session.execute(select(EventConflict).where(EventConflict.event_id == event.id)))
        .scalars()
        .all()
    )
    assert len(conflicts) == 1
    assert "deaths" in conflicts[0].claim
    assert event.conflict_count == 1
    # With an unresolved conflict the event must not be VERIFIED.
    assert event.status != EventStatus.VERIFIED


async def test_stale_number_is_superseded_by_newer_claim(
    db_session, clean_events, make_source, make_report
):
    source_a = await make_source(name="Site A")
    source_b = await make_source(name="Site B")
    source_c = await make_source(name="Site C")

    reports = [
        await make_report(source_a, title="Flood kills 5"),
        await make_report(source_b, title="Flood kills 5"),
        await make_report(source_c, title="Flood kills 12"),
    ]
    for report in reports:
        await cluster_report(db_session, report.id)

    event = (await _events_for_report(db_session, reports[0].id))[0]
    event = await verify_event(db_session, event.id)

    facts = {
        fact.fact_key: fact
        for fact in (
            await db_session.execute(select(EventFact).where(EventFact.event_id == event.id))
        ).scalars()
    }
    # The better-supported figure wins and the other points at it.
    assert facts["number:deaths:12"].superseded_by_id is None
    assert facts["number:deaths:5"].superseded_by_id == facts["number:deaths:12"].id


async def test_verification_is_idempotent(db_session, clean_events, make_source, make_report):
    source = await make_source(name="Reuters")
    report = await make_report(
        source,
        title="Magnitude 6.8 earthquake strikes Japan, killing 12",
        description="Officials confirmed 12 deaths.",
    )
    event, _ = await cluster_report(db_session, report.id)

    await verify_event(db_session, event.id)
    first = (
        (await db_session.execute(select(EventFact).where(EventFact.event_id == event.id)))
        .scalars()
        .all()
    )
    await verify_event(db_session, event.id)
    second = (
        (await db_session.execute(select(EventFact).where(EventFact.event_id == event.id)))
        .scalars()
        .all()
    )

    assert len(first) == len(second)


# --- Stage hand-off through the queue -----------------------------------------


async def test_cluster_job_enqueues_verification(
    db_session, clean_events, make_source, make_report
):
    await db_session.execute(delete(ProcessingJob))
    await db_session.commit()

    source = await make_source()
    report = await make_report(source, title="Wildfire forces evacuations in Greece")

    await queue.enqueue(
        db_session,
        job_type=JobType.CLUSTER_EVENT,
        idempotency_key=f"cluster_event:{report.id}",
        payload={"report_id": str(report.id)},
    )
    await db_session.commit()

    handled = await scheduler.process_pending_jobs(db_session, worker="test-worker", max_jobs=5)
    assert handled >= 1

    verify_jobs = (
        (
            await db_session.execute(
                select(ProcessingJob).where(ProcessingJob.job_type == JobType.VERIFY_EVENT.value)
            )
        )
        .scalars()
        .all()
    )
    assert verify_jobs, "clustering should enqueue a verification job"

    await db_session.execute(delete(ProcessingJob))
    await db_session.commit()


async def test_cluster_job_is_idempotent_by_report(
    db_session, clean_events, make_source, make_report
):
    await db_session.execute(delete(ProcessingJob))
    await db_session.commit()

    source = await make_source()
    report = await make_report(source, title="Wildfire forces evacuations in Greece")
    key = f"cluster_event:{report.id}"

    first = await queue.enqueue(
        db_session,
        job_type=JobType.CLUSTER_EVENT,
        idempotency_key=key,
        payload={"report_id": str(report.id)},
    )
    second = await queue.enqueue(
        db_session,
        job_type=JobType.CLUSTER_EVENT,
        idempotency_key=key,
        payload={"report_id": str(report.id)},
    )
    await db_session.commit()

    assert first.created is True
    assert second.created is False
    assert first.job.id == second.job.id

    await db_session.execute(delete(ProcessingJob))
    await db_session.commit()
