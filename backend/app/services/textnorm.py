"""Normalization primitives for duplicate detection (spec section 7).

Level 1  canonical URL + hash
Level 2  SHA-256 of normalized content
Level 3  cheap SimHash bucket (near-duplicate candidate key)
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Tracking parameters that never change which story a URL points at.
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "utm_name",
    "utm_reader",
    "gclid",
    "fbclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "ref_url",
    "source",
    "cmpid",
    "ocid",
    "ns_campaign",
    "ns_mchannel",
    "ns_source",
    "at_medium",
    "at_campaign",
    "spm",
    "yclid",
}

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)


def canonicalize_url(url: str) -> str:
    """Reduce a URL to a stable canonical form for Level 1 deduplication."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"

    host = parts.hostname.lower() if parts.hostname else ""
    port = parts.port
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        host = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_pairs))

    return urlunsplit((scheme, host, path, query, ""))


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_url_hash(url: str) -> str:
    return sha256_hex(canonicalize_url(url))


def normalize_text(text: str) -> str:
    """Normalize prose so trivially different reports hash identically."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _PUNCT.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def content_hash(title: str, body: str) -> str:
    """Level 2: SHA-256 over normalized title + body."""
    return sha256_hex(f"{normalize_text(title)}\n{normalize_text(body)}")


def _tokens(text: str) -> list[str]:
    return normalize_text(text).split()


def simhash(text: str, bits: int = 64) -> str:
    """Level 3: 64-bit SimHash, hex-encoded, for cheap near-duplicate bucketing."""
    vector = [0] * bits
    for token in _tokens(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        token_hash = int.from_bytes(digest, "big")
        for bit in range(bits):
            vector[bit] += 1 if (token_hash >> bit) & 1 else -1
    value = 0
    for bit in range(bits):
        if vector[bit] > 0:
            value |= 1 << bit
    return f"{value:016x}"


def hamming_distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")
