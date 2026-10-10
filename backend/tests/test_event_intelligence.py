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


# The offline hashing provider is a lexical stand-in, not a semantic model, so
# this is a regression guard for how well it must at least do: paraphrase pairs
# that describe one event must land at/above the clustering threshold once the
# entity and number signals from `facts.py` are added. See
# `test_clustering_db.py` for the same pairs driven through `cluster_report`.
_PARAPHRASE_PAIRS = [
    ("Earthquake strikes Japan killing 12", "Twelve killed after earthquake hits Japan"),
    ("Earthquake strikes Japan killing 12", "Japan earthquake: 12 dead"),
    ("Magnitude 6.8 earthquake strikes Japan", "Strong 6.8 quake hits Japan"),
    ("Flooding in Bangladesh displaces thousands", "Thousands displaced by Bangladesh floods"),
    ("Wildfire forces evacuations in California", "California wildfire prompts evacuation orders"),
    ("Earthquake strikes Japan killing 12", "Quake in Japan leaves twelve dead"),
]


def _combined_similarity(a: str, b: str) -> float:
    """Reproduce the clustering score for two headlines, offline."""
    import asyncio

    from app.services.clustering import W_ENTITY, W_NUMBER, W_SEMANTIC

    provider = HashingEmbeddingProvider(1536)

    def signals(text: str):
        facts = extract_facts(text, "")
        entities = frozenset(
            f.value_text.casefold() for f in facts if f.fact_type == "entity" and f.value_text
        )
        numbers = frozenset(f.fact_key for f in facts if f.fact_type == "number")
        return entities, numbers

    def jaccard(x, y):
        return len(x & y) / len(x | y) if x | y else 0.0

    va, vb = asyncio.run(provider.embed([a, b]))
    semantic = max(0.0, cosine_similarity(va, vb))
    ea, na = signals(a)
    eb, nb = signals(b)
    return W_SEMANTIC * semantic + W_ENTITY * jaccard(ea, eb) + W_NUMBER * jaccard(na, nb)


@pytest.mark.parametrize(("a", "b"), _PARAPHRASE_PAIRS)
def test_offline_embedder_clusters_paraphrases(a, b):
    """Every paraphrase pair clears the default threshold offline (spec 7)."""
    from app.core.config import settings

    assert _combined_similarity(a, b) >= settings.cluster_similarity_threshold


def test_offline_embedder_keeps_distinct_events_apart():
    """The recall fix must not merge genuinely different events."""
    from app.core.config import settings

    distinct = [
        ("Earthquake strikes Japan killing 12", "Earthquake strikes Chile killing 12"),
        ("Earthquake strikes Japan killing 12", "Japan election: polls open"),
        ("Flooding in Bangladesh displaces thousands", "Wildfire forces evacuations in California"),
        ("Magnitude 6.8 earthquake strikes Japan", "Magnitude 5.1 earthquake hits Greece"),
    ]
    for a, b in distinct:
        assert _combined_similarity(a, b) < settings.cluster_similarity_threshold


def test_number_words_fold_to_digits_in_the_embedding():
    import asyncio

    provider = HashingEmbeddingProvider(512)
    digits = asyncio.run(provider.embed(["12 killed in the quake"]))[0]
    words = asyncio.run(provider.embed(["twelve killed in the earthquake"]))[0]
    # "killed"->death and "quake"->earthquake fold both sides onto one token set.
    assert cosine_similarity(digits, words) > 0.9


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


def test_number_words_yield_the_same_claim_as_digits():
    words = {f.fact_key for f in extract_facts("Twelve killed after earthquake hits Japan")}
    digits = {f.fact_key for f in extract_facts("12 killed after earthquake hits Japan")}
    assert "number:deaths:12" in words
    assert "number:deaths:12" in digits


def test_single_word_country_is_an_entity():
    keys = {f.fact_key for f in extract_facts("Earthquake strikes Japan killing 12")}
    assert "entity:japan" in keys


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
