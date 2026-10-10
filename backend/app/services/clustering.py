"""Event clustering — event identity (spec sections 8-9).

Many reports describe one real-world event. This module decides, for each newly
ingested report, whether it belongs to an existing event or starts a new one.

The decision uses several signals, not just semantic similarity, because
embeddings alone will happily merge "earthquake in Japan" with "earthquake in
Chile". Signals:

* **semantic similarity** — cosine similarity of report and event embeddings.
* **event type** — a hard compatibility gate: a quake never merges with an
  election.
* **entities** — overlap of named entities (places, organizations).
* **numbers** — overlap of extracted quantities (a shared casualty figure is
  strong evidence of the same event).
* **time** — reports must fall inside the event's time window.

The result is a weighted score; a report joins the best-matching event when it
clears the configured threshold, otherwise a new event is created.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.enums import EventStatus, SourceReportStatus
from app.models.event import Event, EventReport, EventUpdate
from app.models.source import Source
from app.models.source_report import SourceReport
from app.services.embeddings import cosine_similarity, embed_texts
from app.services.facts import extract_facts
from app.services.textnorm import simhash

logger = get_logger(__name__)

# Weights for the combined similarity score (sum to 1.0). Semantic similarity
# of the headlines dominates — two reports about the same event usually share
# the same headline meaning — while entity and number overlap add supporting
# evidence and help separate same-topic but different events.
W_SEMANTIC = 0.80
W_ENTITY = 0.15
W_NUMBER = 0.05

# Candidates are bounded so clustering stays O(recent events), not O(all events).
MAX_CANDIDATES = 300


@dataclass(frozen=True)
class ReportSignals:
    """Everything clustering needs about one report, computed once."""

    report_id: uuid.UUID
    title: str
    embedding: list[float]
    event_type: str | None
    entities: frozenset[str]
    numbers: frozenset[str]
    occurred_at: datetime


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def signals_from_report(report: SourceReport, embedding: list[float]) -> ReportSignals:
    facts = extract_facts(report.title, report.description or "")
    entities = frozenset(
        f.value_text.casefold() for f in facts if f.fact_type == "entity" and f.value_text
    )
    numbers = frozenset(f.fact_key for f in facts if f.fact_type == "number")
    occurred = report.published_at or report.retrieved_at or datetime.now(UTC)
    return ReportSignals(
        report_id=report.id,
        title=report.title,
        embedding=embedding,
        event_type=report.event_type,
        entities=entities,
        numbers=numbers,
        occurred_at=occurred,
    )


def similarity(report: ReportSignals, event: Event, event_signals: ReportSignals) -> float:
    """Weighted similarity between a report and an event (0-1).

    A type mismatch is a penalty, not a hard veto: keyword classifiers are
    unreliable (the same headline can read as "crime" or "conflict"), so two
    near-identical headlines must still be allowed to merge even when their
    labels differ. A shared event with genuinely different types stays below
    the threshold on semantics alone.
    """
    if event.embedding is None:
        return 0.0
    semantic = max(0.0, cosine_similarity(report.embedding, event.embedding))
    entity = _jaccard(report.entities, event_signals.entities)
    number = _jaccard(report.numbers, event_signals.numbers)
    score = W_SEMANTIC * semantic + W_ENTITY * entity + W_NUMBER * number
    if (
        report.event_type is not None
        and event.event_type is not None
        and report.event_type != event.event_type
    ):
        score *= settings.cluster_type_mismatch_penalty
    return score


def within_time_window(a: datetime, b: datetime) -> bool:
    window = timedelta(hours=settings.event_time_window_hours)
    return abs((a - b).total_seconds()) <= window.total_seconds()


async def _event_signals(session: AsyncSession, event: Event) -> ReportSignals | None:
    """Reconstruct the entity/number signals of an event from its primary report."""
    primary = (
        await session.execute(
            select(SourceReport)
            .join(EventReport, EventReport.source_report_id == SourceReport.id)
            .where(EventReport.event_id == event.id)
            .order_by(EventReport.is_primary.desc(), SourceReport.retrieved_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if primary is None:
        return None
    facts = extract_facts(primary.title, primary.description or "")
    entities = frozenset(
        f.value_text.casefold() for f in facts if f.fact_type == "entity" and f.value_text
    )
    numbers = frozenset(f.fact_key for f in facts if f.fact_type == "number")
    return ReportSignals(
        report_id=primary.id,
        title=primary.title,
        embedding=event.embedding or [],
        event_type=event.event_type,
        entities=entities,
        numbers=numbers,
        occurred_at=event.last_updated_at,
    )


async def _candidate_events(session: AsyncSession, report: ReportSignals) -> list[Event]:
    """Time-bounded events, nearest to the report's embedding first.

    Ordering by vector distance (not recency) is what keeps the candidate cap
    safe: during a burst a recency-ordered top-N silently drops the event a
    report actually belongs to, splitting one event into several.
    """
    window = timedelta(hours=settings.event_time_window_hours)
    since = report.occurred_at - window
    stmt = (
        select(Event)
        .where(
            Event.last_updated_at >= since,
            Event.status != EventStatus.ARCHIVED,
            Event.embedding.is_not(None),
        )
        .order_by(Event.embedding.cosine_distance(report.embedding))
        .limit(MAX_CANDIDATES)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _recompute_centroid(session: AsyncSession, event: Event) -> None:
    """Average member embeddings into a normalized centroid."""
    vectors = (
        (
            await session.execute(
                select(SourceReport.embedding)
                .join(EventReport, EventReport.source_report_id == SourceReport.id)
                .where(EventReport.event_id == event.id, SourceReport.embedding.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    vectors = [list(v) for v in vectors if v is not None]
    if not vectors:
        return
    dim = len(vectors[0])
    centroid = [0.0] * dim
    for vector in vectors:
        for i, value in enumerate(vector):
            centroid[i] += value
    norm = sum(v * v for v in centroid) ** 0.5
    if norm:
        centroid = [v / norm for v in centroid]
    event.embedding = centroid


async def _attach(
    session: AsyncSession,
    event: Event,
    report: SourceReport,
    score: float,
) -> None:
    session.add(
        EventReport(
            event_id=event.id,
            source_report_id=report.id,
            similarity_score=round(score, 4),
            is_primary=False,
        )
    )
    event.last_updated_at = datetime.now(UTC)
    if event.status in (EventStatus.DETECTED, EventStatus.CLUSTERING):
        event.status = EventStatus.VERIFYING
    session.add(
        EventUpdate(
            event_id=event.id,
            occurred_at=datetime.now(UTC),
            update_type="report_added",
            summary=f"New report attached from {report.title[:120]}",
            source_report_id=report.id,
        )
    )


async def _create_event(
    session: AsyncSession, report: SourceReport, source: Source, signals: ReportSignals
) -> Event:
    now = datetime.now(UTC)
    event = Event(
        event_type=report.event_type,
        title=report.title[:500],
        country=source.country,
        first_detected_at=signals.occurred_at,
        last_updated_at=now,
        importance_score=0.0,
        confidence_score=0.0,
        status=EventStatus.DETECTED,
        embedding=signals.embedding,
        simhash=simhash(f"{report.title} {report.description or ''}"),
    )
    session.add(event)
    await session.flush()
    session.add(
        EventReport(
            event_id=event.id,
            source_report_id=report.id,
            similarity_score=1.0,
            is_primary=True,
        )
    )
    session.add(
        EventUpdate(
            event_id=event.id,
            occurred_at=now,
            update_type="detected",
            summary=f"Event first detected from {source.name}",
            source_report_id=report.id,
        )
    )
    logger.info("created event %s (%s)", event.id, event.event_type or "untyped")
    return event


async def ensure_embedding(session: AsyncSession, report: SourceReport) -> list[float]:
    """Return the report embedding, computing and persisting it if absent.

    Embeds the headline only. Bodies are often syndicated near-verbatim between
    sources, which would make unrelated stories look alike; the headline is the
    most reliable short signal of which event a report is about.
    """
    if report.embedding is not None:
        return list(report.embedding)
    [vector] = await embed_texts([report.title])
    report.embedding = vector
    if report.event_type is None:
        from app.services.facts import extract_event_type

        report.event_type = extract_event_type(report.title, report.description or "")
    return vector


async def cluster_report(session: AsyncSession, report_id: uuid.UUID) -> tuple[Event, bool]:
    """Attach a report to an event, creating one if nothing matches.

    Returns the event and whether it was newly created.
    """
    report = await session.get(SourceReport, report_id)
    if report is None:
        raise ValueError(f"source report {report_id} no longer exists")
    source = await session.get(Source, report.source_id)
    if source is None:
        raise ValueError(f"source for report {report_id} no longer exists")

    vector = await ensure_embedding(session, report)
    signals = signals_from_report(report, vector)

    best_event: Event | None = None
    best_score = 0.0
    for event in await _candidate_events(session, signals):
        if not within_time_window(signals.occurred_at, event.last_updated_at):
            continue
        event_signals = await _event_signals(session, event)
        if event_signals is None:
            continue
        score = similarity(signals, event, event_signals)
        if score > best_score:
            best_score = score
            best_event = event

    created = False
    if best_event is not None and best_score >= settings.cluster_similarity_threshold:
        event = best_event
        await _attach(session, event, report, best_score)
    else:
        event = await _create_event(session, report, source, signals)
        created = True

    await _recompute_centroid(session, event)
    report.status = SourceReportStatus.PROCESSED
    report.processed_at = datetime.now(UTC)
    await session.flush()
    return event, created
