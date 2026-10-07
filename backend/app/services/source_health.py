"""Source health and reliability bookkeeping (spec sections 27 and 33).

Health is operational (is the feed reachable, how fast, how often does it fail);
reliability is editorial (how much do we trust what it says). They are tracked
separately so a flaky-but-accurate source is not silently demoted, and a
fast-but-wrong one is not silently promoted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.source import Source, SourceHealth

logger = get_logger(__name__)

# Consecutive failures before a source is considered degraded / down.
DEGRADED_AFTER = 3
DOWN_AFTER = 6
# Latency (ms) above which a success still counts as degraded.
SLOW_LATENCY_MS = 10_000.0
# Weight of the newest latency sample in the rolling average.
LATENCY_SMOOTHING = 0.3


@dataclass(frozen=True)
class HealthSnapshot:
    status: str
    consecutive_failures: int
    success_count: int
    failure_count: int
    avg_latency_ms: float | None
    last_error: str | None


def _now() -> datetime:
    return datetime.now(UTC)


async def _get_or_create(session: AsyncSession, source_id) -> SourceHealth:  # type: ignore[no-untyped-def]
    health = (
        await session.execute(select(SourceHealth).where(SourceHealth.source_id == source_id))
    ).scalar_one_or_none()
    if health is None:
        health = SourceHealth(source_id=source_id, status="unknown")
        session.add(health)
        await session.flush()
    return health


def _roll_latency(previous: float | None, sample: float) -> float:
    if previous is None:
        return sample
    return (LATENCY_SMOOTHING * sample) + ((1 - LATENCY_SMOOTHING) * previous)


async def record_success(
    session: AsyncSession,
    source: Source,
    *,
    latency_ms: float | None = None,
    item_count: int | None = None,
) -> SourceHealth:
    """Record a successful poll: reset the failure streak, update latency."""
    health = await _get_or_create(session, source.id)
    health.consecutive_failures = 0
    health.success_count += 1
    health.last_checked_at = _now()
    health.last_error = None
    if latency_ms is not None:
        health.avg_latency_ms = _roll_latency(health.avg_latency_ms, latency_ms)

    if health.avg_latency_ms is not None and health.avg_latency_ms > SLOW_LATENCY_MS:
        health.status = "degraded"
    else:
        health.status = "healthy"

    source.last_success_at = health.last_checked_at
    await session.flush()
    logger.debug("source %s ok (%s ms, %s items)", source.slug, latency_ms, item_count)
    return health


async def record_failure(
    session: AsyncSession,
    source: Source,
    *,
    error: str,
    latency_ms: float | None = None,
) -> SourceHealth:
    """Record a failed poll and escalate status without stopping the worker."""
    health = await _get_or_create(session, source.id)
    health.consecutive_failures += 1
    health.failure_count += 1
    health.last_checked_at = _now()
    health.last_error = error[:2000]
    if latency_ms is not None:
        health.avg_latency_ms = _roll_latency(health.avg_latency_ms, latency_ms)

    if health.consecutive_failures >= DOWN_AFTER:
        health.status = "down"
    elif health.consecutive_failures >= DEGRADED_AFTER:
        health.status = "degraded"
    else:
        # A single transient failure is not worth alarming on, but the source is
        # no longer "healthy" until the next success clears the streak.
        health.status = "degraded"

    source.last_failure_at = health.last_checked_at
    await session.flush()
    logger.warning(
        "source %s failed (%d in a row): %s",
        source.slug,
        health.consecutive_failures,
        error,
    )
    return health


async def snapshot(session: AsyncSession, source_id) -> HealthSnapshot | None:  # type: ignore[no-untyped-def]
    health = (
        await session.execute(select(SourceHealth).where(SourceHealth.source_id == source_id))
    ).scalar_one_or_none()
    if health is None:
        return None
    return HealthSnapshot(
        status=health.status,
        consecutive_failures=health.consecutive_failures,
        success_count=health.success_count,
        failure_count=health.failure_count,
        avg_latency_ms=health.avg_latency_ms,
        last_error=health.last_error,
    )
