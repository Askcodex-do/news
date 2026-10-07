from __future__ import annotations

from app.services.feed_parser import parse_feed_bytes, strip_html
from app.services.textnorm import canonicalize_url


def test_parse_feed_returns_entries(sample_rss_bytes):
    entries = parse_feed_bytes(sample_rss_bytes)
    assert len(entries) == 2
    first = entries[0]
    assert first.title == "Earthquake strikes Japan killing 12"
    assert first.canonical_url == canonicalize_url("https://example.com/story?id=123")
    assert first.published_at is not None
    assert first.published_at.year == 2025
    assert first.language == "en"


def test_html_is_stripped_from_description(sample_rss_bytes):
    entries = parse_feed_bytes(sample_rss_bytes)
    assert "<p>" not in entries[0].description
    assert "magnitude 6.8" in entries[0].description


def test_strip_html_handles_none():
    assert strip_html(None) == ""


def test_entries_without_link_or_title_are_skipped():
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title></title><link>https://e.com/x</link></item>
    <item><title>Has title</title><link></link></item>
    <item><title>Good</title><link>https://e.com/good</link></item>
    </channel></rss>"""
    entries = parse_feed_bytes(rss)
    assert [e.title for e in entries] == ["Good"]
