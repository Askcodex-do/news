"""Continuous worker loop (spec sections 25-27).

A single loop that, on every tick:

1. returns jobs abandoned by dead workers to PENDING,
2. executes any runnable jobs,
3. periodically enqueues due sources.

Scheduling and execution share this process in Phase 2. They are separate
functions, so splitting them into dedicated processes later is a deployment
change, not a rewrite.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.services import queue, scheduler

logger = get_logger(__name__)

# How often to look for runnable jobs.
TICK_SECONDS = 10
# How often to scan for sources that are due for a poll.
SCHEDULE_EVERY_SECONDS = 60
# Jobs executed per tick before yielding.
MAX_JOBS_PER_TICK = 25


async def run_ingestion_loop(
    stop: asyncio.Event,
    *,
    tick_seconds: int = TICK_SECONDS,
    schedule_every_seconds: int = SCHEDULE_EVERY_SECONDS,
) -> None:
    """Run until ``stop`` is set. Never exits because of a single failure."""
    worker = queue.worker_id()
    logger.info("ingestion worker %s starting", worker)
    elapsed_since_schedule = schedule_every_seconds  # schedule on the first tick

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
                async with SessionLocal() as session:
                    await scheduler.enqueue_due_sources(session)
                elapsed_since_schedule = 0

            if ran:
                logger.debug("worker %s handled %d job(s)", worker, ran)
        except Exception:  # noqa: BLE001 - the loop must survive any single failure
            logger.exception("worker %s tick failed; continuing", worker)

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        elapsed_since_schedule += tick_seconds

    logger.info("ingestion worker %s stopped", worker)


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
