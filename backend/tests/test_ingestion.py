"""Unit tests for ingestion validation and scheduler helpers.

These exercise real functions with real inputs; no database or network needed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.source import Source
from app.services.feed_parser import ParsedEntry
from app.services.ingestion import IngestStats, validate_entry
from app.services.scheduler import _bucket, _next_due, _poll_key


def _entry(**overrides) -> ParsedEntry:  # type: ignore[no-untyped-def]
    base = {
        "title": "Earthquake strikes Japan killing 12",
        "description": "A magnitude 6.8 earthquake hit Japan.",
        "link": "https://example.com/story?id=123",
        "canonical_url": "https://example.com/story?id=123",
        "published_at": datetime(2025, 10, 6, 9, 0, tzinfo=UTC),
        "author": "newsroom",
        "language": "en",
    }
    base.update(overrides)
    return ParsedEntry(**base)


def test_valid_entry_passes():
    assert validate_entry(_entry()) is None


def test_missing_title_is_rejected():
    assert validate_entry(_entry(title="   ")) == "empty title"


def test_missing_link_is_rejected():
    assert validate_entry(_entry(link="", canonical_url="")) == "empty link"


def test_non_http_link_is_rejected():
    assert validate_entry(_entry(canonical_url="javascript:alert(1)")) == "non-http(s) link"


def test_far_future_published_at_is_rejected():
    future = datetime.now(UTC) + timedelta(days=2)
    assert validate_entry(_entry(published_at=future)) == "published_at implausibly in the future"


def test_overlong_title_is_rejected():
    assert validate_entry(_entry(title="x" * 1001)) == "title too long"


def test_ingest_stats_report_duplicates_per_level():
    # Duplicates are reported as separate counters so the dashboard can attribute
    # them to the level that caught them.
    stats = IngestStats(fetched=5, stored=3, duplicate_url=1, duplicate_content=1)
    assert stats.as_dict() == {
        "fetched": 5,
        "stored": 3,
        "duplicate_url": 1,
        "duplicate_content": 1,
        "duplicate_batch": 0,
        "rejected": 0,
    }


def _source(**overrides) -> Source:  # type: ignore[no-untyped-def]
    values = {
        "slug": "test-src",
        "name": "Test Source",
        "website_url": "https://example.com",
        "poll_interval_seconds": 300,
    }
    values.update(overrides)
    return Source(**values)


def test_bucket_is_stable_within_the_polling_window():
    t0 = datetime(2025, 10, 6, 9, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=120)  # still inside the 300s window
    assert _bucket(t0, 300) == _bucket(t1, 300)


def test_bucket_changes_across_windows():
    t0 = datetime(2025, 10, 6, 9, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=400)
    assert _bucket(t0, 300) != _bucket(t1, 300)


def test_poll_key_is_deterministic_within_a_window():
    when = datetime(2025, 10, 6, 9, 0, tzinfo=UTC)
    source = _source()
    assert _poll_key(source, when) == _poll_key(source, when + timedelta(seconds=30))
    assert _poll_key(source, when).startswith("poll_source:test-src:")


def test_next_due_uses_configured_interval_for_healthy_source():
    now = datetime(2025, 10, 6, 9, 0, tzinfo=UTC)
    source = _source(poll_interval_seconds=300, last_success_at=now - timedelta(seconds=200))
    assert _next_due(source, now=now, failures=0) == source.last_success_at + timedelta(seconds=300)


def test_next_due_backs_off_for_failing_source():
    now = datetime(2025, 10, 6, 9, 0, tzinfo=UTC)
    source = _source(poll_interval_seconds=300, last_failure_at=now)
    # 3 consecutive failures => 3x interval before the next attempt.
    assert _next_due(source, now=now, failures=3) == now + timedelta(seconds=900)


def test_next_due_backoff_is_capped():
    now = datetime(2025, 10, 6, 9, 0, tzinfo=UTC)
    source = _source(poll_interval_seconds=60, last_failure_at=now)
    # Capped at 8x regardless of how many failures have accumulated.
    assert _next_due(source, now=now, failures=100) == now + timedelta(seconds=480)


def test_next_due_never_polled_is_due_immediately():
    now = datetime(2025, 10, 6, 9, 0, tzinfo=UTC)
    assert _next_due(_source(), now=now, failures=0) == now
