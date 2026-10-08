"""Periodic housekeeping for a 24/7 deployment (spec sections 22, 25).

Two safe, idempotent sweeps, each a single statement:

* :func:`archive_stale_events` — a developing event with no new reports for a
  configured window is archived, so the feed does not carry week-old "breaking"
  stories forever.
* :func:`prune_finished_jobs` — deletes SUCCEEDED jobs past retention. Dead
  jobs are deliberately kept: they are the evidence a human needs to diagnose a
  source or model that keeps failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.enums import EventStatus
from app.models.event import Event
from app.services import queue

logger = get_logger(__name__)

# States that represent an active, not-yet-closed story. PUBLISHED is included:
# a published developing story is still updated until it goes quiet.
_ACTIVE_STATUSES = (
    EventStatus.DETECTED,
    EventStatus.CLUSTERING,
    EventStatus.VERIFYING,
    EventStatus.VERIFIED,
    EventStatus.EDITORIAL_REVIEW,
    EventStatus.PUBLISHED,
    EventStatus.UPDATING,
)


@dataclass
class MaintenanceResult:
    events_archived: int = 0
    jobs_pruned: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"events_archived": self.events_archived, "jobs_pruned": self.jobs_pruned}


async def archive_stale_events(
    session: AsyncSession, *, now: datetime | None = None, older_than_hours: int | None = None
) -> int:
    """Archive active events with no update within the window. Returns the count."""
    now = now or datetime.now(UTC)
    hours = settings.stale_event_hours if older_than_hours is None else older_than_hours
    cutoff = now - timedelta(hours=hours)
    result = await session.execute(
        update(Event)
        .where(Event.status.in_(_ACTIVE_STATUSES), Event.last_updated_at < cutoff)
        .values(status=EventStatus.ARCHIVED, last_updated_at=now)
    )
    count = int(result.rowcount or 0)
    if count:
        logger.info("archived %d stale event(s) (older than %dh)", count, hours)
    return count


async def prune_finished_jobs(
    session: AsyncSession, *, now: datetime | None = None, retention_days: int | None = None
) -> int:
    """Delete SUCCEEDED jobs past retention. Returns the count."""
    days = settings.job_retention_days if retention_days is None else retention_days
    count = await queue.purge_finished_jobs(session, older_than=timedelta(days=days))
    if count:
        logger.info("pruned %d finished job(s) (retention %dd)", count, days)
    return count


async def run_maintenance(
    session: AsyncSession, *, now: datetime | None = None
) -> MaintenanceResult:
    """Run every sweep. Each is independent and safe to repeat."""
    now = now or datetime.now(UTC)
    result = MaintenanceResult(
        events_archived=await archive_stale_events(session, now=now),
        jobs_pruned=await prune_finished_jobs(session, now=now),
    )
    await session.commit()
    return result


async def count_active_events(session: AsyncSession) -> int:
    """Observability helper: how many events are still live."""
    from sqlalchemy import func

    return int(
        (
            await session.execute(
                select(func.count()).select_from(Event).where(Event.status.in_(_ACTIVE_STATUSES))
            )
        ).scalar_one()
    )
