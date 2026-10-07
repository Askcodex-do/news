from __future__ import annotations

import pytest

from app.services.config_loader import (
    EXPECTED_INTERNATIONAL_COUNT,
    load_all_sources,
    load_country_sources,
    load_international_sources,
    load_local_sources,
)


def test_exactly_thirty_international_sources():
    sources = load_international_sources()
    assert len(sources) == EXPECTED_INTERNATIONAL_COUNT


def test_source_ids_are_unique_across_all_files():
    ids = [s.id for s in load_all_sources()]
    assert len(ids) == len(set(ids))


def test_every_feed_source_has_a_url():
    for source in load_all_sources():
        assert source.website_url
        if source.type.value in {"rss", "atom"}:
            assert source.rss_url, source.id


def test_country_mappings_reference_known_sources():
    known = {s.id for s in load_all_sources()}
    mappings = load_country_sources()
    assert mappings, "expected at least one country mapping"
    for mapping in mappings:
        assert mapping.primary in known, mapping.country_code
        if mapping.backup:
            assert mapping.backup in known


def test_specific_required_mappings_exist():
    by_country = {m.country_code: m.primary for m in load_country_sources()}
    assert by_country["IN"] == "indiatoday"
    assert by_country["PK"] == "dawn"
    assert by_country["JP"] == "nhk-world"
    assert by_country["GB"] == "bbc-world"


def test_priorities_and_intervals_are_in_range():
    for source in load_all_sources():
        assert 0 <= source.priority <= 100
        assert 0 <= source.reliability_score <= 100
        assert source.poll_interval_seconds >= 30


def test_local_sources_file_is_loadable():
    assert load_local_sources()


@pytest.mark.parametrize("bad", [{"country_code": "IND"}, {"country_code": "1N"}])
def test_country_code_validation_rejects_bad_values(bad):
    from pydantic import ValidationError

    from app.services.config_loader import CountryMapping

    with pytest.raises(ValidationError):
        CountryMapping(primary="x", **bad)
