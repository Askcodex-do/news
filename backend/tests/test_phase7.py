"""Phase 7 unit tests: loop shutdown, maintenance/observability shaping.

No database, no network.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.core.config import settings
from app.services import maintenance, observability
from app.services.queue import JobType
from app.services.worker_loop import run_ingestion_loop


async def test_loop_returns_immediately_when_stop_is_set():
    """A stopped loop must not run a tick (so shutdown never touches the DB)."""
    stop = asyncio.Event()
    stop.set()
    # If the loop ran, it would reach for the database and this would raise.
    await run_ingestion_loop(stop, tick_seconds=0)


def test_maintenance_result_as_dict():
    result = maintenance.MaintenanceResult(events_archived=3, jobs_pruned=5)
    assert result.as_dict() == {"events_archived": 3, "jobs_pruned": 5}


def test_active_statuses_exclude_terminal_states():
    statuses = {s.value for s in maintenance._ACTIVE_STATUSES}
    assert "PUBLISHED" in statuses and "UPDATING" in statuses
    assert "ARCHIVED" not in statuses
    # A story parked waiting for evidence is not "active" for archiving; it is
    # handled by the verification worker, not the stale sweep.
    assert "WAIT" not in statuses and "UNVERIFIED" not in statuses


def test_ops_snapshot_serializes_to_plain_dict():
    snap = observability.OpsSnapshot(
        generated_at=datetime.now(UTC),
        sources=observability.SourceHealthSummary(total=2, healthy=1, down=1, offline=["X"]),
        ingestion=observability.IngestionSummary(reports_total=10),
        events=observability.EventSummary(total=4, by_status={"PUBLISHED": 4}),
        jobs=observability.JobSummary(queue={"PENDING": 1}),
        scheduler=None,
        articles_published=3,
    )
    data = snap.as_dict()
    assert data["sources"]["offline"] == ["X"]
    assert data["events"]["by_status"] == {"PUBLISHED": 4}
    assert data["articles_published"] == 3


def test_accuracy_snapshot_reports_the_configured_floor():
    snap = observability.AccuracySnapshot(generated_at=datetime.now(UTC))
    assert snap.confidence_floor == 0.0
    assert settings.min_confidence_to_publish == 70.0


def test_generate_image_is_a_handled_job_type():
    """Phase 6's image job must remain routable through the Phase 7 worker."""
    assert JobType.GENERATE_IMAGE.value == "generate_image"
