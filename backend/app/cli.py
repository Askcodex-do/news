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
    args = parser.parse_args()

    if args.command == "seed":
        asyncio.run(_seed())
    elif args.command == "export-seeds":
        _export_seeds()


if __name__ == "__main__":
    main()
