"""Seed the database from the YAML configuration files.

Idempotent: re-running updates existing rows by slug rather than duplicating.
Called by `python -m app.cli seed` and by the compose bootstrap.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.country import Country
from app.models.source import CountrySource, Source, SourceHealth
from app.services.config_loader import (
    SourceConfig,
    load_all_sources,
    load_country_sources,
    load_international_sources,
)

logger = get_logger(__name__)

# Minimal country reference. `is_supported` is set when a mapping exists.
COUNTRIES: list[dict[str, str]] = [
    {"code": "US", "name": "United States", "region": "Americas", "default_language": "en"},
    {"code": "GB", "name": "United Kingdom", "region": "Europe", "default_language": "en"},
    {"code": "IN", "name": "India", "region": "Asia", "default_language": "en"},
    {"code": "PK", "name": "Pakistan", "region": "Asia", "default_language": "en"},
    {"code": "JP", "name": "Japan", "region": "Asia", "default_language": "ja"},
    {"code": "CN", "name": "China", "region": "Asia", "default_language": "zh"},
    {"code": "RU", "name": "Russia", "region": "Europe", "default_language": "ru"},
    {"code": "DE", "name": "Germany", "region": "Europe", "default_language": "de"},
    {"code": "FR", "name": "France", "region": "Europe", "default_language": "fr"},
    {"code": "IT", "name": "Italy", "region": "Europe", "default_language": "it"},
    {"code": "ES", "name": "Spain", "region": "Europe", "default_language": "es"},
    {"code": "BR", "name": "Brazil", "region": "Americas", "default_language": "pt"},
    {"code": "CA", "name": "Canada", "region": "Americas", "default_language": "en"},
    {"code": "AU", "name": "Australia", "region": "Oceania", "default_language": "en"},
    {"code": "QA", "name": "Qatar", "region": "Middle East", "default_language": "ar"},
    {"code": "TR", "name": "Turkey", "region": "Middle East", "default_language": "tr"},
    {"code": "IL", "name": "Israel", "region": "Middle East", "default_language": "he"},
    {"code": "SG", "name": "Singapore", "region": "Asia", "default_language": "en"},
    {"code": "HK", "name": "Hong Kong", "region": "Asia", "default_language": "en"},
    {"code": "KR", "name": "South Korea", "region": "Asia", "default_language": "ko"},
    {"code": "ID", "name": "Indonesia", "region": "Asia", "default_language": "id"},
    {"code": "MY", "name": "Malaysia", "region": "Asia", "default_language": "en"},
    {"code": "TH", "name": "Thailand", "region": "Asia", "default_language": "th"},
    {"code": "PH", "name": "Philippines", "region": "Asia", "default_language": "en"},
    {"code": "UA", "name": "Ukraine", "region": "Europe", "default_language": "uk"},
    {"code": "IE", "name": "Ireland", "region": "Europe", "default_language": "en"},
    {"code": "NL", "name": "Netherlands", "region": "Europe", "default_language": "nl"},
    {"code": "MX", "name": "Mexico", "region": "Americas", "default_language": "es"},
    {"code": "NG", "name": "Nigeria", "region": "Africa", "default_language": "en"},
    {"code": "ZA", "name": "South Africa", "region": "Africa", "default_language": "en"},
    {"code": "KE", "name": "Kenya", "region": "Africa", "default_language": "en"},
    {"code": "AR", "name": "Argentina", "region": "Americas", "default_language": "es"},
]


async def _upsert_source(
    session: AsyncSession, config: SourceConfig, *, is_international: bool
) -> Source:
    result = await session.execute(select(Source).where(Source.slug == config.id))
    source = result.scalar_one_or_none()
    values = {
        "name": config.name,
        "country": config.country,
        "type": config.type,
        "rss_url": str(config.rss_url) if config.rss_url else None,
        "website_url": str(config.website_url),
        "api_url": str(config.api_url) if config.api_url else None,
        "enabled": config.enabled,
        "priority": config.priority,
        "language": config.language,
        "poll_interval_seconds": config.poll_interval_seconds,
        "reliability_score": config.reliability_score,
        "is_international": is_international,
        "raw_text_permitted": config.raw_text_permitted,
    }
    if source is None:
        source = Source(slug=config.id, **values)
        session.add(source)
        await session.flush()
        session.add(SourceHealth(source_id=source.id, status="unknown"))
    else:
        for key, value in values.items():
            setattr(source, key, value)
    return source


async def seed(session: AsyncSession) -> None:
    international = load_international_sources()
    local = load_all_sources()[len(international) :]
    international_ids = {s.id for s in international}

    for country in COUNTRIES:
        existing = await session.get(Country, country["code"])
        if existing is None:
            session.add(Country(**country, is_supported=False))
        else:
            for key, value in country.items():
                setattr(existing, key, value)

    for config in international + local:
        await _upsert_source(session, config, is_international=config.id in international_ids)

    await session.flush()

    for mapping in load_country_sources():
        result = await session.execute(select(Source).where(Source.slug == mapping.primary))
        source = result.scalar_one_or_none()
        if source is None:
            raise ValueError(
                f"country {mapping.country_code} maps to unknown source {mapping.primary!r}"
            )

        country = await session.get(Country, mapping.country_code)
        if country is None:
            session.add(
                Country(code=mapping.country_code, name=mapping.country_code, is_supported=True)
            )
        else:
            country.is_supported = mapping.enabled

        existing = await session.execute(
            select(CountrySource).where(
                CountrySource.country_code == mapping.country_code,
                CountrySource.source_id == source.id,
            )
        )
        row = existing.scalar_one_or_none()
        if row is None:
            session.add(
                CountrySource(
                    country_code=mapping.country_code,
                    source_id=source.id,
                    role="primary",
                    priority=mapping.priority,
                    enabled=mapping.enabled,
                )
            )
        else:
            row.priority = mapping.priority
            row.enabled = mapping.enabled
            row.role = "primary"

    await session.commit()
    logger.info(
        "seed complete: %d international + %d local sources, %d country mappings",
        len(international),
        len(local),
        len(load_country_sources()),
    )
