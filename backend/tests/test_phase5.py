"""Phase 5 unit tests: ranking, geolocation backend, localization prompt.

These exercise the real functions with no database and no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.models.article import Article
from app.services.ai import build_article_prompt
from app.services.geoip import (
    GLOBAL_COUNTRY,
    MaxMindGeoIPProvider,
    NullGeoIPProvider,
    StaticGeoIPProvider,
    build_geoip_provider,
    resolve_country,
)
from app.services.ranking import RankSignals, rank_articles, rank_score, recency_factor

_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def _article(*, importance: float, confidence: float, age_hours: float) -> Article:
    return Article(
        importance_score=importance,
        confidence_score=confidence,
        published_at=_NOW - timedelta(hours=age_hours),
    )


# --- recency -----------------------------------------------------------------


def test_recency_factor_halves_every_half_life():
    half_life = settings.rank_recency_half_life_hours
    fresh = recency_factor(_NOW, now=_NOW)
    one_life = recency_factor(_NOW - timedelta(hours=half_life), now=_NOW)
    two_lives = recency_factor(_NOW - timedelta(hours=2 * half_life), now=_NOW)
    assert fresh == 1.0
    assert abs(one_life - 0.5) < 1e-9
    assert abs(two_lives - 0.25) < 1e-9


def test_recency_factor_missing_timestamp_is_zero_not_error():
    assert recency_factor(None, now=_NOW) == 0.0


# --- ranking -----------------------------------------------------------------


def test_importance_dominates_recency():
    important_but_older = _article(importance=95, confidence=90, age_hours=20)
    minor_but_fresh = _article(importance=35, confidence=90, age_hours=0)
    ranked = rank_articles([(minor_but_fresh, False), (important_but_older, False)], now=_NOW)
    assert ranked[0] is important_but_older


def test_recency_breaks_ties_at_equal_importance():
    newer = _article(importance=70, confidence=80, age_hours=1)
    older = _article(importance=70, confidence=80, age_hours=30)
    ranked = rank_articles([(older, False), (newer, False)], now=_NOW)
    assert ranked == [newer, older]


def test_ranking_is_deterministic_and_capped():
    articles = [(_article(importance=50 + i, confidence=80, age_hours=i), False) for i in range(30)]
    first = rank_articles(articles, now=_NOW, limit=20)
    second = rank_articles(articles, now=_NOW, limit=20)
    assert len(first) == 20
    assert [a.importance_score for a in first] == [a.importance_score for a in second]


def test_local_bonus_lifts_but_cannot_beat_a_major_story():
    local_minor = _article(importance=40, confidence=80, age_hours=0)
    global_major = _article(importance=98, confidence=95, age_hours=0)
    local_score = rank_score(RankSignals(40, 80, local_minor.published_at, is_local=True), now=_NOW)
    global_score = rank_score(
        RankSignals(98, 95, global_major.published_at, is_local=False), now=_NOW
    )
    # Local bonus is a nudge, not a way to promote weak news over real news.
    assert local_score > rank_score(
        RankSignals(40, 80, local_minor.published_at, is_local=False), now=_NOW
    )
    assert global_score > local_score


def test_score_is_bounded_0_to_100():
    top = rank_score(RankSignals(100, 100, _NOW, is_local=False), now=_NOW)
    assert 0.0 <= top <= 100.0
    bottom = rank_score(RankSignals(-5, -5, None, is_local=False), now=_NOW)
    assert bottom == 0.0


# --- geolocation backend selection -------------------------------------------


def test_factory_defaults_to_null_provider():
    assert isinstance(build_geoip_provider("null"), NullGeoIPProvider)
    assert isinstance(build_geoip_provider(""), NullGeoIPProvider)
    assert isinstance(build_geoip_provider("unknown-backend"), NullGeoIPProvider)


def test_factory_builds_static_provider_from_json_map():
    provider = build_geoip_provider("static", static_map='{"203.0.113.0/24": "IN"}')
    assert isinstance(provider, StaticGeoIPProvider)
    assert resolve_country("203.0.113.7", provider).country_code == "IN"


def test_factory_bad_static_map_falls_back_to_null():
    provider = build_geoip_provider("static", static_map="{not json")
    assert isinstance(provider, NullGeoIPProvider)
    assert resolve_country("203.0.113.7", provider).country_code == GLOBAL_COUNTRY


def test_factory_missing_maxmind_db_falls_back_to_null():
    provider = build_geoip_provider("maxmind", database_path="")
    assert isinstance(provider, NullGeoIPProvider)


def test_maxmind_provider_requires_a_path():
    import pytest

    with pytest.raises(ValueError):
        MaxMindGeoIPProvider("")


# --- localization prompt (spec section 18) -----------------------------------


def test_global_prompt_has_no_audience_block():
    system, _user = build_article_prompt({"event": {}}, None)
    assert "Target audience" not in system


def test_localized_prompt_frames_audience_without_allowing_fabrication():
    system, _user = build_article_prompt({"event": {}}, "IN")
    assert "readers in IN" in system
    # The explicit prohibition is the point: localization may re-angle, not invent.
    assert "Never invent or imply local impact" in system
