from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_client_ip, get_geoip_provider
from app.db.session import get_session
from app.models.article import Article
from app.models.source import CountrySource, Source
from app.schemas import ArticleCard, FeedResponse
from app.services.geoip import GLOBAL_COUNTRY, resolve_country

router = APIRouter(tags=["feed"])

# The primary feed shows at most 20 international + 20 local stories (section 21).
FEED_LIMIT = 20


def _published_articles_stmt():
    return (
        select(Article)
        .where(Article.is_published.is_(True))
        .order_by(Article.importance_score.desc(), Article.published_at.desc())
        .limit(FEED_LIMIT)
    )


@router.get("/feed", response_model=FeedResponse)
async def feed(
    request: Request,
    session: AsyncSession = Depends(get_session),
    country: str | None = Query(default=None, description="Override detected country (ISO-2)"),
) -> FeedResponse:
    """Global top-20 plus a country-specific top-20 local feed."""
    if country:
        country_code = country.upper()
        resolved = True
    else:
        geo = resolve_country(get_client_ip(request), get_geoip_provider())
        country_code, resolved = geo.country_code, geo.resolved

    global_articles = list(
        (await session.execute(_published_articles_stmt().where(Article.is_global.is_(True))))
        .scalars()
        .all()
    )

    local_articles: list[Article] = []
    local_source_name: str | None = None

    if country_code != GLOBAL_COUNTRY:
        # Only countries with a configured mapping get a local edition; everyone
        # else silently falls back to the global feed.
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
            local_articles = list(
                (
                    await session.execute(
                        _published_articles_stmt().where(
                            Article.is_global.is_(False),
                            Article.locale_country == country_code,
                        )
                    )
                )
                .scalars()
                .all()
            )

    return FeedResponse(
        country_code=country_code,
        resolved=resolved,
        global_articles=[ArticleCard.model_validate(a) for a in global_articles],
        local_articles=[ArticleCard.model_validate(a) for a in local_articles],
        local_source_name=local_source_name,
    )
