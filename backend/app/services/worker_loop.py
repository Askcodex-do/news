"""Continuous worker loop (spec sections 25-27).

A single loop that, on every tick:

1. returns jobs abandoned by dead workers to PENDING,
2. executes any runnable jobs,
3. periodically, one worker (holding a lease) enqueues due sources,
4. periodically runs housekeeping (archive stale events, prune finished jobs).

Scheduling and execution are separate functions in the same process so that
splitting them into dedicated processes later is a deployment change, not a
rewrite. The scheduling lease means N replicas scale *execution* without N-way
scheduling churn (spec section 25).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import timedelta

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.services import maintenance, queue, scheduler

logger = get_logger(__name__)

# How often to look for runnable jobs.
TICK_SECONDS = 10
# Jobs executed per tick before yielding.
MAX_JOBS_PER_TICK = 25


async def run_ingestion_loop(
    stop: asyncio.Event,
    *,
    tick_seconds: int = TICK_SECONDS,
    schedule_every_seconds: int | None = None,
    maintenance_every_seconds: int | None = None,
    scheduler_lease_seconds: int | None = None,
) -> None:
    """Run until ``stop`` is set. Never exits because of a single failure."""
    schedule_every_seconds = schedule_every_seconds or settings.scheduler_interval_seconds
    maintenance_every_seconds = maintenance_every_seconds or settings.maintenance_interval_seconds
    lease = timedelta(seconds=scheduler_lease_seconds or settings.scheduler_lease_seconds)

    worker = queue.worker_id()
    logger.info("ingestion worker %s starting", worker)
    elapsed_since_schedule = schedule_every_seconds  # schedule on the first tick
    elapsed_since_maintenance = maintenance_every_seconds

    while not stop.is_set():
        try:
            async with SessionLocal() as session:
                await queue.recover_stale(session)
                await session.commit()

            async with SessionLocal() as session:
                ran = await scheduler.process_pending_jobs(
                    session, worker=worker, max_jobs=MAX_JOBS_PER_TICK
                )

            if elapsed_since_schedule >= schedule_every_seconds:
                await _maybe_schedule(worker, lease)
                elapsed_since_schedule = 0

            if elapsed_since_maintenance >= maintenance_every_seconds:
                async with SessionLocal() as session:
                    await maintenance.run_maintenance(session)
                elapsed_since_maintenance = 0

            if ran:
                logger.debug("worker %s handled %d job(s)", worker, ran)
        except Exception:  # noqa: BLE001 - the loop must survive any single failure
            logger.exception("worker %s tick failed; continuing", worker)

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        elapsed_since_schedule += tick_seconds
        elapsed_since_maintenance += tick_seconds

    logger.info("ingestion worker %s stopped", worker)


async def _maybe_schedule(worker: str, lease: timedelta) -> None:
    """Enqueue due sources if this worker holds (or can take) the lease.

    Only one worker schedules at a time; the rest skip this tick and go straight
    back to executing jobs. Losing the race is expected, not an error.
    """
    async with SessionLocal() as session:
        acquired = await queue.acquire_scheduler_lease(session, worker=worker, lease=lease)
        await session.commit()
    if not acquired:
        return

    async with SessionLocal() as session:
        result = await scheduler.enqueue_due_sources(session)
        summary = f"due={result.due} enqueued={result.enqueued}"
        await queue.release_scheduler_lease(session, worker=worker, result=summary)
        await session.commit()


def install_signal_handlers(stop: asyncio.Event) -> None:
    """Stop cleanly on SIGINT/SIGTERM so in-flight work finishes."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # e.g. Windows
            loop.add_signal_handler(sig, stop.set)


async def run_worker(*, tick_seconds: int = TICK_SECONDS) -> None:
    stop = asyncio.Event()
    install_signal_handlers(stop)
    await run_ingestion_loop(stop, tick_seconds=tick_seconds)
