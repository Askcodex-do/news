from __future__ import annotations

from app.services.textnorm import (
    canonical_url_hash,
    canonicalize_url,
    content_hash,
    hamming_distance,
    normalize_text,
    simhash,
)


def test_tracking_params_and_fragments_are_stripped():
    a = canonicalize_url("https://example.com/story?id=123&utm_source=x#top")
    b = canonicalize_url("https://example.com/story?id=123")
    assert a == b


def test_http_is_upgraded_and_host_is_lowercased():
    assert canonicalize_url("http://EXAMPLE.com/Path/") == "https://example.com/Path"


def test_query_is_sorted_for_stability():
    a = canonicalize_url("https://e.com/s?b=2&a=1")
    b = canonicalize_url("https://e.com/s?a=1&b=2")
    assert a == b


def test_canonical_url_hash_is_stable_across_tracking_params():
    assert canonical_url_hash("https://e.com/s?id=1&utm_medium=y") == canonical_url_hash(
        "https://e.com/s?id=1"
    )


def test_content_hash_ignores_case_and_punctuation():
    a = content_hash("Earthquake strikes Japan!", "Twelve people were killed.")
    b = content_hash("earthquake strikes japan", "twelve people were killed")
    assert a == b


def test_normalize_text_collapses_whitespace():
    assert normalize_text("  Hello   WORLD  ") == "hello world"


def test_simhash_near_duplicates_are_close():
    a = simhash("Earthquake strikes Japan killing 12")
    b = simhash("Twelve people killed after earthquake hits Japan")
    c = simhash("Stock markets rally on tech earnings beat")
    assert hamming_distance(a, b) < hamming_distance(a, c)


def test_simhash_is_deterministic():
    assert simhash("same text") == simhash("same text")
