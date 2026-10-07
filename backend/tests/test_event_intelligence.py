"""Unit tests for Phase 3 event intelligence (no database required).

Covers the deterministic pieces: embeddings, fact extraction, independence,
confidence scoring and the clustering similarity function.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime

import pytest

from app.models.enums import ConfidenceBand
from app.services.clustering import ReportSignals
from app.services.confidence import (
    ConfidenceInputs,
    confidence_band,
    is_publishable,
    score_confidence,
)
from app.services.embeddings import (
    HashingEmbeddingProvider,
    cosine_similarity,
    set_embedding_provider,
)
from app.services.facts import classify_event_type, extract_facts
from app.services.independence import collapse_near_duplicates, count_independent_sources

# --- Embeddings ---------------------------------------------------------------


def test_hashing_embedding_is_deterministic_and_normalized():
    provider = HashingEmbeddingProvider(64)
    import asyncio

    first = asyncio.run(provider.embed(["earthquake strikes japan"]))[0]
    second = asyncio.run(provider.embed(["earthquake strikes japan"]))[0]
    assert first == second
    assert len(first) == 64
    assert math.isclose(sum(v * v for v in first), 1.0, rel_tol=1e-9)


def test_similar_text_scores_higher_than_unrelated_text():
    provider = HashingEmbeddingProvider(512)

    async def embed(text: str) -> list[float]:
        return (await provider.embed([text]))[0]

    import asyncio

    a = asyncio.run(embed("earthquake strikes japan killing 12 people"))
    b = asyncio.run(embed("twelve killed after earthquake hits japan"))
    c = asyncio.run(embed("parliament passes budget after long debate"))

    assert cosine_similarity(a, b) > cosine_similarity(a, c)


# --- Fact extraction ----------------------------------------------------------


def test_extracts_casualty_numbers_with_stable_keys():
    facts = extract_facts("Earthquake strikes Japan killing 12", "Officials said 12 people died.")
    keys = {f.fact_key for f in facts}
    assert "number:deaths:12" in keys


def test_number_measure_aliases_collapse():
    killed = {f.fact_key for f in extract_facts("12 killed in blast")}
    dead = {f.fact_key for f in extract_facts("12 dead in blast")}
    assert "number:deaths:12" in killed
    assert "number:deaths:12" in dead


def test_conflicting_numbers_produce_different_keys():
    five = {f.fact_key for f in extract_facts("5 people killed")}
    twelve = {f.fact_key for f in extract_facts("12 people killed")}
    assert "number:deaths:5" in five
    assert "number:deaths:12" in twelve


def test_magnitude_is_extracted():
    keys = {f.fact_key for f in extract_facts("Magnitude 6.8 earthquake hits Japan")}
    assert "number:magnitude:6.8" in keys


def test_dates_are_extracted_in_iso_form():
    keys = {f.fact_key for f in extract_facts("Quake hit on 6 October 2025")}
    assert "date:2025-10-06" in keys


def test_bare_numbers_without_a_measure_are_ignored():
    keys = {f.fact_key for f in extract_facts("The report number 4 was published")}
    assert not any(key.startswith("number:") for key in keys)


def test_event_type_classification():
    assert classify_event_type("Earthquake strikes Japan") == "earthquake"
    assert classify_event_type("Parliament passes new budget") == "politics"
    assert classify_event_type("Nothing notable happened") is None


# --- Independence -------------------------------------------------------------


class _FakeSource:
    def __init__(self, sid, lineage_root=None):
        import uuid

        self.id = sid if isinstance(sid, uuid.UUID) else uuid.uuid4()
        self.lineage_root_id = lineage_root


def test_sources_sharing_a_lineage_count_once():
    import uuid

    root = uuid.uuid4()
    a = _FakeSource(uuid.uuid4(), lineage_root=root)
    b = _FakeSource(uuid.uuid4(), lineage_root=root)
    c = _FakeSource(uuid.uuid4())
    assert count_independent_sources([a, b, c]) == 2


def test_near_duplicate_reports_collapse_to_one_representative():
    import uuid

    provider = HashingEmbeddingProvider(512)

    import asyncio

    a = asyncio.run(provider.embed(["earthquake strikes japan killing 12 people"]))[0]
    b = asyncio.run(provider.embed(["earthquake strikes japan killing 12 people"]))[0]
    c = asyncio.run(provider.embed(["parliament passes the budget"]))[0]

    reps = collapse_near_duplicates([(uuid.uuid4(), a), (uuid.uuid4(), b), (uuid.uuid4(), c)])
    assert len(reps) == 2


def test_reports_without_embeddings_are_not_collapsed():
    import uuid

    reps = collapse_near_duplicates([(uuid.uuid4(), None), (uuid.uuid4(), None)])
    assert len(reps) == 2


# --- Confidence ---------------------------------------------------------------


def _inputs(**overrides) -> ConfidenceInputs:
    base = {
        "independent_sources": 3,
        "total_sources": 3,
        "mean_reliability": 85.0,
        "agreement": 1.0,
        "conflicts": 0,
        "age_hours": 1.0,
        "official_confirmation": False,
        "factual_consistency": 1.0,
    }
    base.update(overrides)
    return ConfidenceInputs(**base)


def test_more_independent_sources_raises_confidence():
    low = score_confidence(_inputs(independent_sources=1, total_sources=1))
    high = score_confidence(_inputs(independent_sources=5, total_sources=5))
    assert high > low


def test_conflicts_reduce_confidence():
    clean = score_confidence(_inputs())
    conflicted = score_confidence(_inputs(conflicts=2))
    assert conflicted < clean


def test_official_confirmation_raises_confidence():
    plain = score_confidence(_inputs())
    official = score_confidence(_inputs(official_confirmation=True))
    assert official > plain


def test_stale_information_decays_confidence():
    fresh = score_confidence(_inputs(age_hours=1.0))
    stale = score_confidence(_inputs(age_hours=24 * 7))
    assert stale < fresh


def test_confidence_never_reaches_perfect_certainty():
    assert score_confidence(_inputs(independent_sources=20, total_sources=20)) < 100.0


def test_confidence_bands_map_correctly():
    assert confidence_band(98) == ConfidenceBand.HIGHLY_CONFIRMED
    assert confidence_band(90) == ConfidenceBand.STRONGLY_SUPPORTED
    assert confidence_band(75) == ConfidenceBand.REASONABLY_SUPPORTED
    assert confidence_band(60) == ConfidenceBand.UNCERTAIN
    assert confidence_band(20) == ConfidenceBand.DO_NOT_PUBLISH


def test_single_source_is_not_publishable_by_default():
    score = score_confidence(_inputs(independent_sources=1, total_sources=1))
    assert not is_publishable(score)


def test_invalid_inputs_are_rejected():
    with pytest.raises(ValueError):
        _inputs(agreement=1.5)
    with pytest.raises(ValueError):
        _inputs(independent_sources=-1)


# --- Clustering similarity ----------------------------------------------------


def test_type_mismatch_is_a_penalty_not_a_veto():
    from app.services.clustering import W_SEMANTIC, similarity

    class _Event:
        embedding = [1.0, 0.0]
        event_type = "conflict"

    signals = ReportSignals(
        report_id=uuid.uuid4(),
        title="t",
        embedding=[1.0, 0.0],
        event_type="crime",
        entities=frozenset(),
        numbers=frozenset(),
        occurred_at=datetime.now(UTC),
    )
    mismatched = ReportSignals(
        report_id=uuid.uuid4(),
        title="t",
        embedding=[1.0, 0.0],
        event_type="conflict",
        entities=frozenset(),
        numbers=frozenset(),
        occurred_at=datetime.now(UTC),
    )
    # Same embedding but a different type label is only penalized, not vetoed:
    # an identical headline still clears the default threshold.
    score = similarity(signals, _Event(), mismatched)
    assert score == pytest.approx(W_SEMANTIC * 0.90)
    assert score >= 0.70


def test_time_window_boundary():
    from app.services.clustering import within_time_window

    now = datetime.now(UTC)
    assert within_time_window(now, now)
    assert not within_time_window(now, datetime(2000, 1, 1, tzinfo=UTC))


@pytest.fixture(autouse=True)
def _reset_embedding_provider():
    yield
    set_embedding_provider(None)
