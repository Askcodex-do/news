"""Independent-source verification (spec section 11).

Ten websites are not ten confirmations if they all republished one wire report.
Two mechanisms reduce a pile of reports to the number of *independent reporting
chains* behind them:

1. **Lineage.** Sources that share a ``lineage_root_id`` (a wire/agency root,
   or an explicit editorial dependency) count once.
2. **Near-duplicate collapse.** Even without declared lineage, reports whose
   embeddings are almost identical are treated as one chain — the fingerprint of
   a copied or syndicated story.

Confidence is built on the resulting count, never on raw report volume.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from app.core.config import settings
from app.models.source import Source
from app.services.embeddings import cosine_similarity


def lineage_root(source: Source) -> uuid.UUID:
    """The reporting chain a source belongs to. Self-rooted when undeclared."""
    return source.lineage_root_id or source.id


def count_independent_sources(sources: Iterable[Source]) -> int:
    """Number of distinct reporting chains among ``sources``."""
    return len({lineage_root(source) for source in sources})


def independent_lineages(sources: Iterable[Source]) -> set[uuid.UUID]:
    return {lineage_root(source) for source in sources}


def collapse_near_duplicates(
    items: Sequence[tuple[uuid.UUID, list[float] | None]],
    *,
    threshold: float | None = None,
) -> list[uuid.UUID]:
    """Return one representative id per near-duplicate group.

    ``items`` is ``(report_id, embedding)``. Reports without an embedding are
    kept as their own group (we cannot prove they are copies). Order is
    preserved so the result is deterministic.
    """
    limit = threshold if threshold is not None else settings.near_duplicate_threshold
    representatives: list[uuid.UUID] = []
    representative_vectors: list[list[float]] = []

    for report_id, vector in items:
        if vector is None:
            representatives.append(report_id)
            continue
        duplicate = False
        for existing in representative_vectors:
            if cosine_similarity(vector, existing) >= limit:
                duplicate = True
                break
        if not duplicate:
            representatives.append(report_id)
            representative_vectors.append(vector)
    return representatives


def count_independent_chains(
    reports: Sequence[tuple[uuid.UUID, Source, list[float] | None]],
    *,
    threshold: float | None = None,
) -> int:
    """Independent chains given ``(report_id, source, embedding)`` triples.

    Combines lineage and near-duplicate collapse: a chain is counted once, and
    two reports from different chains that are nonetheless near-identical are
    also counted once (the second adds no independent confirmation).
    """
    representatives = collapse_near_duplicates(
        [(report_id, vector) for report_id, _source, vector in reports],
        threshold=threshold,
    )
    representative_ids = set(representatives)
    roots: set[uuid.UUID] = set()
    for report_id, source, _vector in reports:
        if report_id in representative_ids:
            roots.add(lineage_root(source))
    return len(roots)
