"""RSS/Atom feed fetching and parsing.

Phase 1 provides the fetch + parse + normalize primitives. The ingestion worker
(Phase 2) drives these on a schedule and persists `source_reports`.

Safety:
  * We only fetch http/https URLs.
  * Response size is capped.
  * HTML is stripped from feed-provided bodies before normalization.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import feedparser
import httpx
from dateutil import parser as date_parser

from app.core.config import settings
from app.core.logging import get_logger
from app.services.textnorm import canonicalize_url
from app.services.url_safety import validate_outbound_url

logger = get_logger(__name__)

MAX_FEED_BYTES = 5 * 1024 * 1024  # 5 MB cap
FETCH_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

_HTML_TAG = __import__("re").compile(r"<[^>]+>")


@dataclass(frozen=True)
class ParsedEntry:
    title: str
    description: str
    link: str
    canonical_url: str
    published_at: datetime | None
    author: str | None
    language: str | None


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    return _HTML_TAG.sub(" ", value).replace("&nbsp;", " ").strip()


def _parse_date(entry: dict) -> datetime | None:
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if raw:
            try:
                parsed = date_parser.parse(raw)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except (ValueError, OverflowError):
                continue
    # feedparser exposes a struct_time as `published_parsed`.
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct:
        return datetime(*struct[:6], tzinfo=UTC)
    return None


def parse_feed_bytes(raw: bytes) -> list[ParsedEntry]:
    feed = feedparser.parse(raw)
    if feed.bozo and not feed.entries:
        raise ValueError(f"feed could not be parsed: {feed.get('bozo_exception')}")

    language = None
    if getattr(feed, "feed", None):
        language = feed.feed.get("language")

    entries: list[ParsedEntry] = []
    for entry in feed.entries:
        link = (entry.get("link") or "").strip()
        title = strip_html(entry.get("title"))
        if not link or not title:
            continue
        entries.append(
            ParsedEntry(
                title=title,
                description=strip_html(entry.get("summary") or entry.get("description")),
                link=link,
                canonical_url=canonicalize_url(link),
                published_at=_parse_date(entry),
                author=entry.get("author"),
                language=language,
            )
        )
    return entries


async def fetch_feed(url: str) -> bytes:
    """Fetch a feed, enforcing scheme, host and size limits. Raises on failure."""
    # Scheme + SSRF guard (spec section 34). Raises UnsafeURL (a ValueError).
    validate_outbound_url(url)

    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": settings.http_user_agent},
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        if len(response.content) > MAX_FEED_BYTES:
            raise ValueError(f"feed exceeds {MAX_FEED_BYTES} bytes")
        return response.content


async def fetch_and_parse(url: str) -> list[ParsedEntry]:
    raw = await fetch_feed(url)
    return parse_feed_bytes(raw)
