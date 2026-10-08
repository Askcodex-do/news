"""Structured evidence package (spec section 10).

The AI writer is only ever given facts that deterministic verification already
extracted and attributed. This module assembles that package from the database,
so the writer's input cannot contain a fact no source reported.

Shape (matching the spec's example)::

    {
      "event": {...},
      "confirmed_facts": [{"fact": "...", "sources": ["Reuters", ...],
                           "independent_sources": 3, "last_confirmed_at": "..."}],
      "conflicting_claims": [{"claim": "...", "status": "uncertain"}],
      "attribution": [{"source_name": "...", "url": "...", "is_independent": true}],
      "context": {"confidence_score": 82.0, "importance_score": 98.0, ...}
    }

Facts marked superseded (section 23) are excluded, and facts with no
independent support are labelled so the writer can hedge rather than assert.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event, EventConflict, EventFact, EventFactSource
from app.models.source import Source
from app.models.source_report import SourceReport

# Facts backed by this many independent sources (or an official source) may be
# stated as fact; below it the writer is told the support is thin.
_SOLID_INDEPENDENT_SUPPORT = 2


async def build_evidence_package(
    session: AsyncSession, event: Event, *, audience_country: str | None = None
) -> dict[str, Any]:
    """Assemble the evidence package for one event."""
    facts = (
        (
            await session.execute(
                select(EventFact)
                .where(EventFact.event_id == event.id, EventFact.superseded_by_id.is_(None))
                .order_by(EventFact.fact_type, EventFact.fact_key)
            )
        )
        .scalars()
        .all()
    )

    # Which sources assert each fact, with their names and URLs for attribution.
    fact_ids = [fact.id for fact in facts]
    sources_by_fact: dict[uuid.UUID, list[SourceReport]] = {}
    if fact_ids:
        rows = (
            await session.execute(
                select(EventFactSource.fact_id, SourceReport)
                .join(SourceReport, SourceReport.id == EventFactSource.source_report_id)
                .where(EventFactSource.fact_id.in_(fact_ids))
            )
        ).all()
        for fact_id, report in rows:
            sources_by_fact.setdefault(fact_id, []).append(report)

    confirmed: list[dict[str, Any]] = []
    thin: list[dict[str, Any]] = []
    attribution: dict[str, dict[str, Any]] = {}

    # Source names are looked up once per source rather than once per fact.
    source_ids = {report.source_id for reports in sources_by_fact.values() for report in reports}
    source_names: dict[uuid.UUID, Source] = {}
    for source_id in source_ids:
        source = await session.get(Source, source_id)
        if source is not None:
            source_names[source_id] = source

    for fact in facts:
        reports = sources_by_fact.get(fact.id, [])
        names: list[str] = []
        for report in reports:
            source = source_names.get(report.source_id)
            if source is not None:
                names.append(source.name)
                attribution.setdefault(
                    source.name,
                    {
                        "source_name": source.name,
                        "url": report.canonical_url or report.source_url,
                        "is_independent": True,
                    },
                )
        names = sorted(set(names))
        entry = {
            "fact": fact.statement,
            "fact_type": fact.fact_type,
            "sources": names,
            "independent_sources": fact.independent_source_count,
            "last_confirmed_at": (
                fact.last_confirmed_at.isoformat() if fact.last_confirmed_at else None
            ),
        }
        if fact.independent_source_count >= _SOLID_INDEPENDENT_SUPPORT:
            confirmed.append(entry)
        else:
            thin.append(entry)

    conflicts = (
        (
            await session.execute(
                select(EventConflict).where(
                    EventConflict.event_id == event.id, EventConflict.resolved_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )

    return {
        "event": {
            "id": str(event.id),
            "title": event.title,
            "event_type": event.event_type,
            "country": event.country,
            "location": event.city or event.region or event.country,
            "first_detected_at": event.first_detected_at.isoformat(),
            "last_updated_at": event.last_updated_at.isoformat(),
        },
        "confirmed_facts": confirmed,
        # Reported by only one independent chain: the writer may mention these
        # but must attribute them as a single report, not as established fact.
        "single_source_facts": thin,
        "conflicting_claims": [
            {"claim": conflict.claim, "status": conflict.status or "unresolved"}
            for conflict in conflicts
        ],
        "attribution": list(attribution.values()),
        "context": {
            "confidence_score": event.confidence_score,
            "importance_score": event.importance_score,
            "report_count": event.report_count,
            "independent_source_count": event.independent_source_count,
            "audience_country": audience_country,
        },
    }


def evidence_numbers(package: dict[str, Any]) -> set[float]:
    """Every numeric value the evidence supports, for the deterministic check.

    The validator compares numbers found in a generated article against this
    set; a number outside it is an invented statistic (spec section 29).
    """
    numbers: set[float] = set()
    for bucket in ("confirmed_facts", "single_source_facts"):
        for entry in package.get(bucket, []):
            for token in _number_tokens(str(entry.get("fact", ""))):
                numbers.add(token)
    return numbers


def evidence_text(package: dict[str, Any]) -> str:
    """All evidence text concatenated, for token/name containment checks."""
    parts: list[str] = []
    event = package.get("event") or {}
    parts.append(str(event.get("title") or ""))
    for bucket in ("confirmed_facts", "single_source_facts"):
        for entry in package.get(bucket, []):
            parts.append(str(entry.get("fact") or ""))
    for conflict in package.get("conflicting_claims", []):
        parts.append(str(conflict.get("claim") or ""))
    return " \n ".join(parts).casefold()


def _number_tokens(text: str) -> set[float]:
    values: set[float] = set()
    for match in re.finditer(r"\d+(?:\.\d+)?", text):
        try:
            values.add(float(match.group(0)))
        except ValueError:  # pragma: no cover - regex guarantees numeric
            continue
    return values
