"""Phase 4 unit tests: importance, article validation, evidence helpers.

These exercise the real code paths with no network and no mocks of the logic
under test. The AI provider is a real ``AIProvider`` implementation (the
offline one, or a small local subclass) rather than a patch, so the validation
and generation code runs as written.
"""

from __future__ import annotations

import pytest

from app.services.ai import ArticleDraft, DeterministicAIProvider
from app.services.article_validation import validate_draft
from app.services.evidence import evidence_numbers, evidence_text
from app.services.importance import (
    ImportanceInputs,
    importance_band,
    score_importance,
)


def _evidence(**overrides) -> dict:
    package = {
        "event": {"title": "Earthquake strikes Japan", "event_type": "earthquake"},
        "confirmed_facts": [
            {
                "fact": "Earthquake occurred",
                "sources": ["NHK", "Reuters"],
                "independent_sources": 2,
            },
            {"fact": "Magnitude was 6.8", "sources": ["NHK"], "independent_sources": 1},
            {"fact": "12 people killed", "sources": ["Reuters"], "independent_sources": 2},
        ],
        "single_source_facts": [],
        "conflicting_claims": [],
        "attribution": [{"source_name": "NHK", "url": "https://nhk.example/x"}],
        "context": {"confidence_score": 88.0},
    }
    package.update(overrides)
    return package


# --- Importance (spec sections 13-14) ---


def test_importance_is_independent_of_confidence():
    # A confirmed but minor story: high confidence, low importance.
    minor = score_importance(
        ImportanceInputs(
            event_type="sports",
            measures={},
            location_count=1,
            entity_count=1,
            update_count=1,
            hours_since_first_seen=48.0,
            official_action=False,
        )
    )
    # A developing catastrophe: low update count but severe type + casualties.
    major = score_importance(
        ImportanceInputs(
            event_type="earthquake",
            measures={"magnitude": 7.8, "deaths": 240, "injured": 3000},
            location_count=6,
            entity_count=20,
            update_count=3,
            hours_since_first_seen=1.0,
            official_action=True,
        )
    )
    assert minor < 40
    assert major > 80
    assert importance_band(minor) == "minor"
    assert importance_band(major) == "major"


def test_importance_rises_with_casualties():
    base = dict(
        event_type="earthquake",
        location_count=1,
        entity_count=2,
        update_count=1,
        hours_since_first_seen=2.0,
        official_action=False,
    )
    few = score_importance(ImportanceInputs(measures={"deaths": 2}, **base))
    many = score_importance(ImportanceInputs(measures={"deaths": 200}, **base))
    assert many > few


def test_importance_scores_within_bounds():
    empty = score_importance(
        ImportanceInputs(
            event_type=None,
            measures={},
            location_count=0,
            entity_count=0,
            update_count=0,
            hours_since_first_seen=0.0,
            official_action=False,
        )
    )
    assert 0.0 <= empty <= 100.0


# --- Evidence helpers (spec section 10) ---


def test_evidence_numbers_extracts_supported_values():
    numbers = evidence_numbers(_evidence())
    assert 6.8 in numbers
    assert 12 in numbers


def test_evidence_text_contains_facts():
    text = evidence_text(_evidence())
    assert "magnitude was 6.8" in text


# --- Article validation (spec sections 28-29) ---


def test_validation_accepts_supported_draft():
    draft = ArticleDraft(
        headline="Earthquake strikes Japan",
        body=(
            "An earthquake occurred in Japan. The magnitude was 6.8 and "
            "12 people were killed, according to reports. Officials in Japan "
            "continue to assess the situation across the affected region."
        ),
        key_points=["Magnitude was 6.8"],
    )
    result = validate_draft(draft, _evidence())
    assert result.ok, result.failures


def test_validation_rejects_invented_number():
    draft = ArticleDraft(
        headline="Earthquake strikes Japan",
        body=(
            "An earthquake occurred in Japan and killed 48 people, a figure "
            "far higher than anything reported. The magnitude was 6.8 and the "
            "situation across the region remains under assessment by officials."
        ),
    )
    result = validate_draft(draft, _evidence())
    assert not result.ok
    assert any("numbers not in evidence" in failure for failure in result.failures)


def test_validation_rejects_fabricated_name():
    draft = ArticleDraft(
        headline="Earthquake strikes Japan",
        body=(
            "An earthquake occurred in Japan. Spokesperson Yuki Tanaka said the "
            "magnitude was 6.8 and 12 people were killed in the affected region, "
            "though officials continue to assess the situation."
        ),
    )
    result = validate_draft(draft, _evidence())
    assert not result.ok
    assert any("names not in evidence" in failure for failure in result.failures)


def test_validation_rejects_fabricated_url():
    draft = ArticleDraft(
        headline="Earthquake strikes Japan",
        body=(
            "An earthquake occurred in Japan. See https://fake.example/story for "
            "the full text, where the magnitude was reported as 6.8 and 12 people "
            "were killed according to officials assessing the region."
        ),
    )
    result = validate_draft(draft, _evidence())
    assert not result.ok
    assert any("URL" in failure for failure in result.failures)


def test_validation_rejects_duplicated_paragraph():
    paragraph = (
        "An earthquake occurred in Japan and the magnitude was 6.8 with 12 people "
        "killed according to reports."
    )
    draft = ArticleDraft(headline="Earthquake strikes Japan", body=f"{paragraph}\n\n{paragraph}")
    result = validate_draft(draft, _evidence())
    assert not result.ok
    assert any("duplicated paragraph" in failure for failure in result.failures)


def test_validation_rejects_source_copying():
    source = (
        "An earthquake occurred in Japan. The magnitude was 6.8 and 12 people were "
        "killed, according to reports from the region. Officials continue to assess "
        "the situation as rescue teams work across the affected areas."
    )
    draft = ArticleDraft(headline="Earthquake strikes Japan", body=source)
    result = validate_draft(draft, _evidence(), source_texts=[source])
    assert not result.ok
    assert any("copies" in failure for failure in result.failures)


def test_validation_rejects_empty_body():
    draft = ArticleDraft(headline="Earthquake strikes Japan", body="")
    result = validate_draft(draft, _evidence())
    assert not result.ok


def test_validation_rejects_writer_insufficient_evidence():
    draft = ArticleDraft(headline="x", body="something", insufficient_evidence=True)
    result = validate_draft(draft, _evidence())
    assert not result.ok


# --- Offline provider (real implementation, used as the test writer) ---


@pytest.mark.asyncio
async def test_offline_provider_composes_only_from_evidence():
    provider = DeterministicAIProvider()
    package = _evidence()
    draft = await provider.draft_article(evidence=package)
    assert not draft.insufficient_evidence
    assert "6.8" in draft.body
    # The offline writer must not invent facts: every fact it emits came from
    # the evidence, so the deterministic validator accepts it.
    result = validate_draft(draft, package)
    assert result.ok, result.failures


@pytest.mark.asyncio
async def test_offline_provider_flags_insufficient_evidence():
    provider = DeterministicAIProvider()
    draft = await provider.draft_article(evidence={"event": {"title": "x"}, "confirmed_facts": []})
    assert draft.insufficient_evidence
