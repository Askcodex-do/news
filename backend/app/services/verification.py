"""Multi-source verification (spec sections 10-13, 23-24).

Given an event's clustered reports, this module produces the structured
evidence the AI writer is allowed to use:

* ``event_facts`` — each claim, with how many *independent* sources assert it
  and when it was last confirmed (stale-information protection, section 23).
* ``event_conflicts`` — claims the sources disagree about (section 24). We never
  silently pick a number.
* ``events.confidence_score`` — how sure we are the facts are true (section 12).

Nothing here calls an LLM. Every output is derived deterministically from the
reports, so the evidence package cannot contain a fact no source reported.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.enums import EventStatus
from app.models.event import (
    Event,
    EventConflict,
    EventFact,
    EventFactSource,
    EventReport,
    EventUpdate,
)
from app.models.source import Source
from app.models.source_report import SourceReport
from app.services.confidence import ConfidenceInputs, score_confidence
from app.services.facts import extract_facts
from app.services.independence import count_independent_chains, lineage_root

logger = get_logger(__name__)

# Source names containing these words are treated as official/authoritative for
# the "official confirmation" signal. Configured per deployment in future.
_OFFICIAL_KEYWORDS = (
    "agency",
    "ministry",
    "government",
    "meteorological",
    "department",
    "authority",
    "official",
    "institute",
    "organization",
    "organisation",
    "university",
)


@dataclass
class EventReportRow:
    report: SourceReport
    source: Source
    embedding: list[float] | None


async def _load_event_reports(session: AsyncSession, event_id: uuid.UUID) -> list[EventReportRow]:
    rows = (
        await session.execute(
            select(SourceReport, Source)
            .join(Source, Source.id == SourceReport.source_id)
            .join(EventReport, EventReport.source_report_id == SourceReport.id)
            .where(EventReport.event_id == event_id)
        )
    ).all()
    result: list[EventReportRow] = []
    for report, source in rows:
        embedding = list(report.embedding) if report.embedding is not None else None
        result.append(EventReportRow(report=report, source=source, embedding=embedding))
    return result


def _is_official(source: Source) -> bool:
    name = (source.name or "").casefold()
    return any(keyword in name for keyword in _OFFICIAL_KEYWORDS)


@dataclass
class _FactAggregate:
    key: str
    fact_type: str
    statement: str
    value_numeric: float | None
    value_text: str | None
    reports: list[uuid.UUID]
    lineages: set[uuid.UUID]
    last_confirmed_at: datetime

    @property
    def source_count(self) -> int:
        return len(self.reports)

    @property
    def independent_source_count(self) -> int:
        return len(self.lineages)


def _aggregate_facts(rows: list[EventReportRow]) -> dict[str, _FactAggregate]:
    aggregates: dict[str, _FactAggregate] = {}
    for row in rows:
        for fact in extract_facts(row.report.title, row.report.description or ""):
            aggregate = aggregates.get(fact.fact_key)
            if aggregate is None:
                aggregates[fact.fact_key] = _FactAggregate(
                    key=fact.fact_key,
                    fact_type=fact.fact_type,
                    statement=fact.statement,
                    value_numeric=fact.value_numeric,
                    value_text=fact.value_text,
                    reports=[row.report.id],
                    lineages={lineage_root(row.source)},
                    last_confirmed_at=row.report.retrieved_at,
                )
            else:
                if row.report.id not in aggregate.reports:
                    aggregate.reports.append(row.report.id)
                aggregate.lineages.add(lineage_root(row.source))
                if row.report.retrieved_at > aggregate.last_confirmed_at:
                    aggregate.last_confirmed_at = row.report.retrieved_at
    return aggregates


def _measure_of(fact_key: str) -> str | None:
    # fact keys look like "number:deaths:12"
    parts = fact_key.split(":")
    if len(parts) == 3 and parts[0] == "number":
        return parts[1]
    return None


@dataclass
class _Conflict:
    fact_type: str
    claim: str
    values: list[tuple[str, float, int]]  # (display, value, independent_count)


def _detect_numeric_conflicts(
    aggregates: dict[str, _FactAggregate],
) -> tuple[list[_Conflict], set[str]]:
    """Find measures whose independent sources report materially different values.

    Returns the conflicts and the set of fact keys that are contested.
    """
    by_measure: dict[str, list[_FactAggregate]] = defaultdict(list)
    for aggregate in aggregates.values():
        measure = _measure_of(aggregate.key)
        if measure is not None:
            by_measure[measure].append(aggregate)

    conflicts: list[_Conflict] = []
    contested: set[str] = set()

    for measure, facts in by_measure.items():
        # Distinct values with independent support.
        distinct = sorted({f.value_numeric for f in facts if f.value_numeric is not None})
        if len(distinct) < 2:
            continue

        # Values within relative tolerance are not a real disagreement. Compare
        # the spread (max vs min) against the largest value.
        spread = (distinct[-1] - distinct[0]) / distinct[-1] if distinct[-1] else 0.0
        if spread <= settings.conflict_relative_tolerance:
            continue

        entries: list[tuple[str, float, int]] = []
        for fact in facts:
            if fact.value_numeric is None:
                continue
            numeric = fact.value_numeric
            display = f"{int(numeric)}" if numeric.is_integer() else str(numeric)
            entries.append((display, numeric, fact.independent_source_count))
            contested.add(fact.key)
        entries.sort(key=lambda item: item[1])
        claim = f"sources disagree on {measure}: " + ", ".join(
            f"{display} ({count} independent)" for display, _v, count in entries
        )
        conflicts.append(_Conflict(fact_type="number", claim=claim, values=entries))

    return conflicts, contested


async def _upsert_facts(
    session: AsyncSession, event: Event, aggregates: dict[str, _FactAggregate]
) -> dict[str, EventFact]:
    existing = {
        fact.fact_key: fact
        for fact in (
            await session.execute(
                select(EventFact).where(
                    EventFact.event_id == event.id, EventFact.fact_key.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    }

    now = datetime.now(UTC)
    for key, aggregate in aggregates.items():
        fact = existing.get(key)
        if fact is None:
            fact = EventFact(
                event_id=event.id,
                fact_type=aggregate.fact_type,
                fact_key=key,
                statement=aggregate.statement,
                value_numeric=aggregate.value_numeric,
                value_text=aggregate.value_text,
                first_seen_at=now,
                last_confirmed_at=aggregate.last_confirmed_at,
                source_count=aggregate.source_count,
                independent_source_count=aggregate.independent_source_count,
            )
            session.add(fact)
            await session.flush()
            existing[key] = fact
        else:
            fact.source_count = aggregate.source_count
            fact.independent_source_count = aggregate.independent_source_count
            fact.last_confirmed_at = max(
                fact.last_confirmed_at or aggregate.last_confirmed_at, aggregate.last_confirmed_at
            )

        # Record which reports assert this fact (idempotent on the unique key).
        linked = set(
            (
                await session.execute(
                    select(EventFactSource.source_report_id).where(
                        EventFactSource.fact_id == fact.id
                    )
                )
            )
            .scalars()
            .all()
        )
        for report_id in aggregate.reports:
            if report_id not in linked:
                session.add(EventFactSource(fact_id=fact.id, source_report_id=report_id))
    await session.flush()
    return existing


def _supersede_contested(
    aggregates: dict[str, _FactAggregate], facts: dict[str, EventFact]
) -> None:
    """Point older numeric claims at the newest value for the same measure.

    Implements section 23: when sources later report "12 deaths", the earlier
    "5 deaths" claim is marked superseded rather than continuing to be published
    as current. The newest claim wins (that is what an update means); ties fall
    back to the better-supported value. Cross-source disagreement is still
    recorded as a conflict by the caller, so superseding does not hide it.
    """
    by_measure: dict[str, list[_FactAggregate]] = defaultdict(list)
    for aggregate in aggregates.values():
        measure = _measure_of(aggregate.key)
        if measure is not None:
            by_measure[measure].append(aggregate)

    for facts_in_measure in by_measure.values():
        if len(facts_in_measure) < 2:
            continue
        dominant = max(
            facts_in_measure,
            key=lambda a: (a.last_confirmed_at, a.independent_source_count),
        )
        dominant_row = facts.get(dominant.key)
        if dominant_row is None:
            continue
        for aggregate in facts_in_measure:
            if aggregate.key == dominant.key:
                continue
            row = facts.get(aggregate.key)
            if row is not None:
                row.superseded_by_id = dominant_row.id


async def _upsert_conflicts(session: AsyncSession, event: Event, conflicts: list[_Conflict]) -> int:
    """Persist conflicts, resolving ones that no longer apply. Returns the count."""
    existing = {
        conflict.claim: conflict
        for conflict in (
            await session.execute(select(EventConflict).where(EventConflict.event_id == event.id))
        )
        .scalars()
        .all()
    }
    now = datetime.now(UTC)
    active_claims = {conflict.claim for conflict in conflicts}

    for conflict in conflicts:
        if conflict.claim not in existing:
            session.add(
                EventConflict(
                    event_id=event.id,
                    fact_type=conflict.fact_type,
                    claim=conflict.claim,
                    status="unresolved",
                    detected_at=now,
                )
            )
    for claim, row in existing.items():
        if claim not in active_claims and row.resolved_at is None:
            row.status = "resolved"
            row.resolved_at = now
            row.resolution = "sources now agree"
    await session.flush()
    return len(conflicts)


async def verify_event(session: AsyncSession, event_id: uuid.UUID) -> Event:
    """Recompute facts, conflicts, independence and confidence for one event."""
    event = await session.get(Event, event_id)
    if event is None:
        raise ValueError(f"event {event_id} no longer exists")

    rows = await _load_event_reports(session, event_id)
    if not rows:
        return event

    aggregates = _aggregate_facts(rows)
    facts = await _upsert_facts(session, event, aggregates)

    conflicts, contested = _detect_numeric_conflicts(aggregates)
    _supersede_contested(aggregates, facts)
    conflict_count = await _upsert_conflicts(session, event, conflicts)

    # --- Independent-source counts (section 11) ---
    independent_sources = count_independent_chains(
        [(row.report.id, row.source, row.embedding) for row in rows]
    )
    total_sources = len({row.source.id for row in rows})
    reliability_by_lineage: dict[uuid.UUID, float] = {}
    for row in rows:
        reliability_by_lineage.setdefault(lineage_root(row.source), row.source.reliability_score)
    mean_reliability = (
        sum(reliability_by_lineage.values()) / len(reliability_by_lineage)
        if reliability_by_lineage
        else 0.0
    )
    official = any(_is_official(row.source) for row in rows)

    numeric_facts = [a for a in aggregates.values() if a.fact_type == "number"]
    if numeric_facts:
        agreeing = sum(1 for a in numeric_facts if a.key not in contested)
        agreement = agreeing / len(numeric_facts)
    else:
        agreement = 1.0
    factual_consistency = 1.0 - (len(contested) / len(aggregates)) if aggregates else 1.0

    newest = max(row.report.retrieved_at for row in rows)
    age_hours = max(0.0, (datetime.now(UTC) - newest).total_seconds() / 3600.0)

    confidence = score_confidence(
        ConfidenceInputs(
            independent_sources=independent_sources,
            total_sources=total_sources,
            mean_reliability=mean_reliability,
            agreement=agreement,
            conflicts=conflict_count,
            age_hours=age_hours,
            official_confirmation=official,
            factual_consistency=factual_consistency,
        )
    )

    # --- Denormalized event state ---
    event.report_count = len(rows)
    event.independent_source_count = independent_sources
    event.conflict_count = conflict_count
    event.confidence_score = confidence
    event.last_updated_at = datetime.now(UTC)

    if event.status in (EventStatus.DETECTED, EventStatus.CLUSTERING, EventStatus.VERIFYING):
        if confidence >= settings.min_confidence_to_publish and conflict_count == 0:
            event.status = EventStatus.VERIFIED
        elif confidence < 50:
            event.status = EventStatus.UNVERIFIED
        else:
            event.status = EventStatus.VERIFYING

    session.add(
        EventUpdate(
            event_id=event.id,
            occurred_at=datetime.now(UTC),
            update_type="verified",
            summary=(
                f"Verified: {independent_sources} independent source(s), "
                f"{conflict_count} conflict(s), confidence {confidence:.1f}"
            ),
        )
    )
    await session.flush()
    logger.info(
        "verified event %s: %d reports, %d independent, %d conflicts, confidence %.1f -> %s",
        event.id,
        len(rows),
        independent_sources,
        conflict_count,
        confidence,
        event.status,
    )
    return event
