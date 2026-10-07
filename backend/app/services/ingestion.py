"""News ingestion pipeline (spec sections 6-7).

    fetch -> parse -> normalize -> validate -> deduplicate -> store

Deduplication runs at three levels before a report is stored:

  Level 1  canonical URL   - the same article URL is never ingested twice
  Level 2  content hash    - identical normalized content is a duplicate
  Level 3  SimHash         - a cheap near-duplicate bucket key, stored for the
                             clustering worker (Phase 3) to compare candidates

The pipeline is deliberately conservative: a report with no usable title or URL
is rejected rather than stored, and nothing is fetched over a non-http(s) scheme.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import SourceReportStatus
from app.models.source import Source
from app.models.source_report import SourceReport
from app.services import source_health
from app.services.feed_parser import ParsedEntry, fetch_and_parse
from app.services.textnorm import canonical_url_hash, content_hash, simhash

logger = get_logger(__name__)

# Bounds keep a single malformed feed entry from bloating the database.
MAX_TITLE_LENGTH = 1000
MAX_DESCRIPTION_LENGTH = 20000
MAX_FUTURE_SKEW_SECONDS = 3600


@dataclass
class IngestStats:
    """Per-poll counters, surfaced through the admin API and logs."""

    fetched: int = 0
    stored: int = 0
    duplicate_url: int = 0
    duplicate_content: int = 0
    duplicate_batch: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def considered(self) -> int:
        return self.fetched

    def as_dict(self) -> dict[str, int]:
        return {
            "fetched": self.fetched,
            "stored": self.stored,
            "duplicate_url": self.duplicate_url,
            "duplicate_content": self.duplicate_content,
            "duplicate_batch": self.duplicate_batch,
            "rejected": self.rejected,
        }


def validate_entry(entry: ParsedEntry) -> str | None:
    """Return a rejection reason, or None if the entry is usable.

    Validation is intentionally strict: we would rather skip an entry than store
    a report we cannot attribute or display.
    """
    if not entry.title.strip():
        return "empty title"
    if not entry.link.strip():
        return "empty link"
    if not entry.canonical_url.startswith(("http://", "https://")):
        return "non-http(s) link"
    if len(entry.title) > MAX_TITLE_LENGTH:
        return "title too long"
    if entry.description and len(entry.description) > MAX_DESCRIPTION_LENGTH:
        return "description too long"
    if entry.published_at is not None:
        skew = (entry.published_at - datetime.now(UTC)).total_seconds()
        if skew > MAX_FUTURE_SKEW_SECONDS:
            return "published_at implausibly in the future"
    return None


async def _existing_canonical_url(
    session: AsyncSession, canonical_hash: str
) -> SourceReport | None:
    return (
        await session.execute(
            select(SourceReport).where(SourceReport.canonical_url_hash == canonical_hash)
        )
    ).scalar_one_or_none()


async def _existing_content_hash(session: AsyncSession, digest: str) -> SourceReport | None:
    return (
        await session.execute(
            select(SourceReport)
            .where(SourceReport.content_hash == digest)
            .order_by(SourceReport.retrieved_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def ingest_entries(
    session: AsyncSession,
    source: Source,
    entries: list[ParsedEntry],
    *,
    stats: IngestStats | None = None,
) -> IngestStats:
    """Run one parsed feed batch through validate -> dedup -> store.

    Duplicates are recorded (not silently dropped) so the accuracy dashboard can
    show how much of the stream is republication noise.
    """
    stats = stats or IngestStats()
    stats.fetched = len(entries)

    seen_urls: set[str] = set()
    seen_content: set[str] = set()

    for entry in entries:
        reason = validate_entry(entry)
        if reason is not None:
            stats.rejected += 1
            stats.errors.append(f"{entry.link or '<no link>'}: {reason}")
            continue

        url_hash = canonical_url_hash(entry.canonical_url)

        # Level 1a: duplicate inside this batch.
        if url_hash in seen_urls:
            stats.duplicate_batch += 1
            continue
        seen_urls.add(url_hash)

        # Level 1b: duplicate already stored from any source.
        if await _existing_canonical_url(session, url_hash) is not None:
            stats.duplicate_url += 1
            continue

        digest = content_hash(entry.title, entry.description)

        # Level 2a: identical content inside this batch.
        if digest in seen_content:
            stats.duplicate_batch += 1
            continue
        seen_content.add(digest)

        # Level 2b: identical content already stored from any source.
        if await _existing_content_hash(session, digest) is not None:
            stats.duplicate_content += 1
            continue

        session.add(
            SourceReport(
                source_id=source.id,
                source_url=entry.link,
                canonical_url=entry.canonical_url,
                canonical_url_hash=url_hash,
                title=entry.title,
                description=entry.description or None,
                published_at=entry.published_at,
                retrieved_at=datetime.now(UTC),
                language=(entry.language or source.language or "en")[:16],
                author=(entry.author or None),
                raw_text_if_permitted=entry.description if source.raw_text_permitted else None,
                content_hash=digest,
                # Level 3 bucket key for near-duplicate candidate selection.
                simhash=simhash(f"{entry.title} {entry.description}"),
                status=SourceReportStatus.NEW,
            )
        )
        stats.stored += 1

    await session.flush()
    return stats


async def ingest_source(session: AsyncSession, source: Source) -> IngestStats:
    """Fetch and ingest one source, recording health either way.

    Never raises for an unreachable source: the failure is recorded and the
    caller moves on to the next source (spec section 27).
    """
    started = time.perf_counter()
    try:
        entries = await fetch_and_parse(source.rss_url or source.api_url or "")
    except Exception as exc:  # noqa: BLE001 - a source failure must not crash the worker
        latency = (time.perf_counter() - started) * 1000
        await source_health.record_failure(
            session, source, error=f"{type(exc).__name__}: {exc}", latency_ms=latency
        )
        stats = IngestStats(errors=[f"fetch failed: {exc}"])
        return stats

    stats = await ingest_entries(session, source, entries)
    latency = (time.perf_counter() - started) * 1000
    await source_health.record_success(session, source, latency_ms=latency, item_count=stats.stored)
    logger.info(
        "ingested %s: %d fetched, %d stored, %d dup, %d rejected",
        source.slug,
        stats.fetched,
        stats.stored,
        stats.duplicate_url + stats.duplicate_content + stats.duplicate_batch,
        stats.rejected,
    )
    return stats
