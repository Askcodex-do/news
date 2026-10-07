"""Small operational CLI: `python -m app.cli <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.core.config import REPO_ROOT
from app.db.session import SessionLocal
from app.services.config_loader import (
    load_all_sources,
    load_country_sources,
    load_international_sources,
)
from app.services.seeding import seed

SEEDS_DIR = REPO_ROOT / "database" / "seeds"


async def _seed() -> None:
    async with SessionLocal() as session:
        await seed(session)


async def _worker(tick_seconds: int) -> None:
    from app.services.worker_loop import run_worker

    await run_worker(tick_seconds=tick_seconds)


async def _ingest_once() -> None:
    """One scheduling + execution pass. Useful for cron and for tests."""
    from app.services import queue, scheduler

    worker = queue.worker_id()
    async with SessionLocal() as session:
        scheduled = await scheduler.enqueue_due_sources(session)
        print(f"scheduled {scheduled.enqueued} new poll job(s) ({scheduled.due} due)")
    async with SessionLocal() as session:
        ran = await scheduler.process_pending_jobs(session, worker=worker, max_jobs=1000)
        print(f"processed {ran} job(s)")


async def _poll_source(slug: str) -> None:
    """Poll a single source immediately, bypassing the schedule."""
    from sqlalchemy import select

    from app.models.source import Source
    from app.services.ingestion import ingest_source

    async with SessionLocal() as session:
        source = (
            await session.execute(select(Source).where(Source.slug == slug))
        ).scalar_one_or_none()
        if source is None:
            raise SystemExit(f"no source with slug {slug!r}")
        stats = await ingest_source(session, source)
        await session.commit()
        print(json.dumps(stats.as_dict(), indent=2))
        for error in stats.errors:
            print(f"  ! {error}")


def _export_seeds() -> None:
    """Write deterministic JSON snapshots of the YAML config.

    The YAML files remain the source of truth; these snapshots are reviewable
    artifacts (diffable in PRs) and let other services consume the same data
    without importing Python.
    """
    SEEDS_DIR.mkdir(parents=True, exist_ok=True)

    def dump(name: str, payload: object) -> Path:
        path = SEEDS_DIR / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    sources = [s.model_dump(mode="json") for s in load_all_sources()]
    international_ids = {s.id for s in load_international_sources()}
    for record in sources:
        record["is_international"] = record["id"] in international_ids

    mappings = [m.model_dump(mode="json") for m in load_country_sources()]

    written = [dump("sources.json", sources), dump("country_sources.json", mappings)]
    for path in written:
        print(f"wrote {path.relative_to(REPO_ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="load config/*.yaml into the database")
    sub.add_parser("export-seeds", help="write database/seeds/*.json snapshots from config")
    sub.add_parser("ingest-once", help="run one scheduling + execution pass")
    poll = sub.add_parser("poll-source", help="poll one source by slug immediately")
    poll.add_argument("slug")
    worker = sub.add_parser("worker", help="run the continuous ingestion worker")
    worker.add_argument("--tick-seconds", type=int, default=10)
    args = parser.parse_args()

    if args.command == "seed":
        asyncio.run(_seed())
    elif args.command == "export-seeds":
        _export_seeds()
    elif args.command == "ingest-once":
        asyncio.run(_ingest_once())
    elif args.command == "poll-source":
        asyncio.run(_poll_source(args.slug))
    elif args.command == "worker":
        asyncio.run(_worker(args.tick_seconds))


if __name__ == "__main__":
    main()
