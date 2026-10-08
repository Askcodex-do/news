from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_client_ip, get_geoip_provider
from app.db.session import get_session
from app.schemas import ArticleCard, FeedResponse
from app.services.geoip import GLOBAL_COUNTRY, resolve_country
from app.services.ranking import load_ranked_feed

router = APIRouter(tags=["feed"])


@router.get("/feed", response_model=FeedResponse)
async def feed(
    request: Request,
    session: AsyncSession = Depends(get_session),
    country: str | None = Query(default=None, description="Override detected country (ISO-2)"),
) -> FeedResponse:
    """Ranked global top-20 plus a country-specific local top-20 (spec section 21).

    Country comes from the visitor IP unless overridden. A country with no
    configured mapping, or an unresolvable IP, falls back to the global edition
    and is never blocked (spec section 5).
    """
    if country:
        country_code = country.upper()
        resolved = True
    else:
        geo = resolve_country(get_client_ip(request), get_geoip_provider())
        country_code, resolved = geo.country_code, geo.resolved

    if country_code == GLOBAL_COUNTRY:
        # An unresolved visitor gets the global edition and no local feed.
        global_articles, _, _ = await load_ranked_feed(session, country_code=country_code)
        return FeedResponse(
            country_code=country_code,
            resolved=resolved,
            global_articles=[ArticleCard.model_validate(a) for a in global_articles],
            local_articles=[],
            local_source_name=None,
        )

    global_articles, local_articles, local_source_name = await load_ranked_feed(
        session, country_code=country_code
    )
    return FeedResponse(
        country_code=country_code,
        resolved=resolved,
        global_articles=[ArticleCard.model_validate(a) for a in global_articles],
        local_articles=[ArticleCard.model_validate(a) for a in local_articles],
        local_source_name=local_source_name,
    )
