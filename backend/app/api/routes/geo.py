from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_client_ip, get_geoip_provider
from app.db.session import get_session
from app.models.source import CountrySource, Source
from app.schemas import CountrySourceOut, GeoResponse
from app.services.geoip import GLOBAL_COUNTRY, resolve_country

router = APIRouter(tags=["geo"])


@router.get("/geo", response_model=GeoResponse)
async def geo(request: Request) -> GeoResponse:
    """Resolve the visitor's country from IP. Never fails, never blocks."""
    result = resolve_country(get_client_ip(request), get_geoip_provider())
    return GeoResponse(country_code=result.country_code, resolved=result.resolved)


@router.get("/country-sources", response_model=list[CountrySourceOut])
async def country_sources(
    session: AsyncSession = Depends(get_session),
    country: str | None = Query(default=None, description="Filter by ISO-2 country code"),
) -> list[CountrySourceOut]:
    """The configured country -> local source mapping (public transparency)."""
    stmt = (
        select(CountrySource, Source)
        .join(Source, Source.id == CountrySource.source_id)
        .order_by(CountrySource.country_code, CountrySource.priority.desc())
    )
    if country:
        stmt = stmt.where(CountrySource.country_code == country.upper())
    rows = (await session.execute(stmt)).all()
    return [
        CountrySourceOut(
            country_code=cs.country_code,
            role=cs.role,
            priority=cs.priority,
            enabled=cs.enabled,
            source_slug=src.slug,
            source_name=src.name,
        )
        for cs, src in rows
    ]


__all__ = ["router", "GLOBAL_COUNTRY"]
