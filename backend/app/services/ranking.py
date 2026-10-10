"""Feed ranking (spec section 21).

Every visitor sees two ranked lists: a global feed and a local feed for their
country, each capped at 20 stories.

Ranking is deliberately **not** an engagement score. The spec forbids optimising
for clicks, virality or social popularity, so the score is a transparent,
bounded blend of signals that were already computed upstream:

* **importance** — public safety, casualties, official action, scale (Phase 4).
* **confidence** — how sure we are the facts are true (Phase 3).
* **recency** — stale stories fade, so a developing event outranks an old one
  with the same importance.
* **locality** — for the local feed, a small bonus for stories about (or
  localized to) the visitor's country.

Importance dominates because "what matters" is the editorial question; recency
only breaks ties and fades old coverage. Nothing here can promote a story above
its accuracy: only published, validated articles are ever ranked.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.article import Article
from app.models.event import Event
from app.models.source import CountrySource, Source

# Weights sum to 1.0 so the score is itself a 0-100 quantity.
W_IMPORTANCE = 0.55
W_CONFIDENCE = 0.30
W_RECENCY = 0.15


@dataclass(frozen=True)
class RankSignals:
    """The inputs to a ranking decision for one article."""

    importance_score: float
    confidence_score: float
    published_at: datetime | None
    is_local: bool = False


def recency_factor(published_at: datetime | None, *, now: datetime) -> float:
    """Exponential recency decay in 0-1, halving every configured half-life.

    Missing timestamps score 0 rather than raising: an article with no publish
    time should sink, not crash the feed.
    """
    if published_at is None:
        return 0.0
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    age_hours = max(0.0, (now - published_at).total_seconds() / 3600.0)
    half_life = max(1e-6, settings.rank_recency_half_life_hours)
    return math.pow(0.5, age_hours / half_life)


def rank_score(signals: RankSignals, *, now: datetime) -> float:
    """A bounded 0-100 ranking score. Higher ranks first."""
    base = (
        W_IMPORTANCE * _clamp(signals.importance_score)
        + W_CONFIDENCE * _clamp(signals.confidence_score)
        + W_RECENCY * 100.0 * recency_factor(signals.published_at, now=now)
    )
    if signals.is_local:
        base *= settings.local_relevance_bonus
    return base


def rank_articles(
    articles: Iterable[tuple[Article, bool]],
    *,
    now: datetime,
    limit: int | None = None,
) -> list[Article]:
    """Rank ``(article, is_local)`` pairs, newest-first among equal scores.

    The sort is stable and fully determined by the score plus the publish time,
    so the feed is deterministic for a given set of articles.
    """
    scored = [
        (
            rank_score(
                RankSignals(
                    importance_score=article.importance_score,
                    confidence_score=article.confidence_score,
                    published_at=article.published_at,
                    is_local=is_local,
                ),
                now=now,
            ),
            article.published_at or datetime.min.replace(tzinfo=UTC),
            index,
            article,
        )
        for index, (article, is_local) in enumerate(articles)
    ]
    # Higher score first; then newer publish time; then original order.
    scored.sort(key=lambda row: (row[0], row[1], -row[2]), reverse=True)
    ranked = [row[3] for row in scored]
    return ranked[:limit] if limit is not None else ranked


async def load_ranked_feed(
    session: AsyncSession,
    *,
    country_code: str,
    limit: int | None = None,
) -> tuple[list[Article], list[Article], str | None]:
    """Load the ranked global and local feeds for ``country_code``.

    Returns ``(global_articles, local_articles, local_source_name)``. The local
    feed is empty (and ``local_source_name`` is ``None``) when the country has
    no configured mapping — the visitor simply gets the global edition and is
    never blocked (spec section 5).
    """
    cap = limit if limit is not None else settings.feed_limit
    now = datetime.now(UTC)

    global_rows = (
        await session.execute(
            select(Article, Event.country)
            .join(Event, Event.id == Article.event_id)
            .where(Article.is_published.is_(True), Article.is_global.is_(True))
        )
    ).all()
    global_articles = rank_articles(
        ((article, False) for article, _country in global_rows), now=now, limit=cap
    )

    local_articles: list[Article] = []
    local_source_name: str | None = None

    mapping = (
        (
            await session.execute(
                select(CountrySource)
                .where(
                    CountrySource.country_code == country_code,
                    CountrySource.enabled.is_(True),
                )
                .order_by(CountrySource.priority.desc())
            )
        )
        .scalars()
        .first()
    )
    if mapping is not None:
        local_source = await session.get(Source, mapping.source_id)
        local_source_name = local_source.name if local_source else None

        # Local news is the localized edition for this country plus any event
        # that is about this country. When an event has a localized edition we
        # show that; otherwise we fall back to its global article. Either way a
        # single event appears once, so the local column never shows the same
        # story twice.
        local_rows = (
            await session.execute(
                select(Article, Event.country)
                .join(Event, Event.id == Article.event_id)
                .where(
                    Article.is_published.is_(True),
                    or_(
                        Article.locale_country == country_code,
                        Event.country == country_code,
                    ),
                )
            )
        ).all()

        chosen: dict[object, Article] = {}
        for article, _country in local_rows:
            existing = chosen.get(article.event_id)
            if existing is None or (
                article.locale_country == country_code and existing.locale_country != country_code
            ):
                chosen[article.event_id] = article

        local_articles = rank_articles(
            ((article, True) for article in chosen.values()), now=now, limit=cap
        )

    return global_articles, local_articles, local_source_name


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value or 0.0))
