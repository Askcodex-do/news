"""Original article generation (spec sections 16-19, 22, 28-29).

Turns a verified event into an original article:

    event + evidence package + attribution + audience
        -> AI writer
        -> AI fact-check pass        (section 28, layer 5)
        -> deterministic validation  (section 28, layer 6)
        -> article + version + sources

Nothing is published unless the draft passes validation. The event stays in
``EDITORIAL_REVIEW`` until it does, and a rejected draft leaves the event
unpublished rather than publishing something unsupported (section 35).

Re-generation reuses the same article row and appends a version, so a
developing story keeps ONE article and a full timeline (section 22).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.article import Article, ArticleImage, ArticleSource, ArticleVersion
from app.models.country import Country
from app.models.enums import EventStatus
from app.models.event import Event, EventFact, EventReport
from app.models.source import CountrySource, Source
from app.models.source_report import SourceReport
from app.services.ai import ai_provider_configured, get_ai_provider
from app.services.article_validation import ValidationResult, validate_draft
from app.services.evidence import build_evidence_package
from app.services.importance import ImportanceInputs, score_importance
from app.services.textnorm import normalize_text

logger = get_logger(__name__)

# Body changes smaller than this are not worth a new version; a re-verify that
# changes nothing must not inflate the version history.
_MIN_BODY_CHANGE = 0.02


@dataclass
class GenerationOutcome:
    article_id: uuid.UUID | None
    published: bool
    reason: str
    version: int = 0
    validation: ValidationResult | None = None


def _slugify(text: str) -> str:
    slug = normalize_text(text).replace(" ", "-")
    slug = "".join(ch for ch in slug if ch.isalnum() or ch == "-")
    return slug.strip("-")[:180] or "article"


async def _unique_slug(
    session: AsyncSession, base: str, *, exclude: uuid.UUID | None = None
) -> str:
    slug = base
    suffix = 2
    while True:
        stmt = select(Article.id).where(Article.slug == slug)
        if exclude is not None:
            stmt = stmt.where(Article.id != exclude)
        if (await session.execute(stmt)).scalar_one_or_none() is None:
            return slug
        slug = f"{base}-{suffix}"
        suffix += 1


def _article_stmt(event_id: uuid.UUID, audience_country: str | None):
    """The one article row for an event *in a given locale* (spec section 18).

    ``audience_country=None`` selects the global article; otherwise the
    localized edition for that country. This is what lets an event carry a
    global version plus one version per locale without duplicating the story.
    """
    stmt = select(Article).where(Article.event_id == event_id)
    if audience_country is None:
        return stmt.where(Article.is_global.is_(True))
    return stmt.where(Article.is_global.is_(False), Article.locale_country == audience_country)


async def _source_texts(session: AsyncSession, event_id: uuid.UUID) -> list[str]:
    """Report titles+bodies for the copy-detection check (spec section 17)."""
    rows = (
        await session.execute(
            select(SourceReport.title, SourceReport.description)
            .join(EventReport, EventReport.source_report_id == SourceReport.id)
            .where(EventReport.event_id == event_id)
        )
    ).all()
    return [f"{title} {description or ''}" for title, description in rows]


async def _measure_of(session: AsyncSession, event: Event) -> dict[str, float]:
    """Largest reported value per measure, from the event's non-superseded facts."""
    facts = (
        (
            await session.execute(
                select(EventFact).where(
                    EventFact.event_id == event.id,
                    EventFact.superseded_by_id.is_(None),
                    EventFact.fact_type == "number",
                )
            )
        )
        .scalars()
        .all()
    )
    measures: dict[str, float] = {}
    for fact in facts:
        if fact.value_numeric is None or not fact.fact_key:
            continue
        parts = fact.fact_key.split(":")
        if len(parts) != 3 or parts[0] != "number":
            continue
        measure = parts[1]
        current = measures.get(measure)
        if current is None or fact.value_numeric > current:
            measures[measure] = fact.value_numeric
    return measures


async def recompute_importance(session: AsyncSession, event: Event) -> float:
    """Score importance from already-verified facts (spec sections 13-14)."""
    measures = await _measure_of(session, event)

    location_count = len({value for value in (event.city, event.region, event.country) if value})
    entity_count = (
        await session.execute(
            select(func.count(func.distinct(SourceReport.description)))
            .join(EventReport, EventReport.source_report_id == SourceReport.id)
            .where(EventReport.event_id == event.id)
        )
    ).scalar() or 0

    update_count = len(
        (
            await session.execute(
                select(EventReport.source_report_id).where(EventReport.event_id == event.id)
            )
        )
        .scalars()
        .all()
    )

    hours_since_first_seen = max(
        0.0, (datetime.now(UTC) - event.first_detected_at).total_seconds() / 3600.0
    )

    event.importance_score = score_importance(
        ImportanceInputs(
            event_type=event.event_type,
            measures=measures,
            location_count=location_count,
            entity_count=int(entity_count),
            update_count=update_count,
            hours_since_first_seen=hours_since_first_seen,
            official_action=bool(measures.get("deaths") or measures.get("killed")),
        )
    )
    return event.importance_score


async def _upsert_attribution(
    session: AsyncSession, article: Article, event_id: uuid.UUID
) -> list[ArticleSource]:
    """Write the transparent Sources section (spec section 19)."""
    rows = (
        await session.execute(
            select(SourceReport, Source)
            .join(Source, Source.id == SourceReport.source_id)
            .join(EventReport, EventReport.source_report_id == SourceReport.id)
            .where(EventReport.event_id == event_id)
        )
    ).all()

    existing = {
        (row.source_id, row.url): row
        for row in (
            await session.execute(
                select(ArticleSource).where(ArticleSource.article_id == article.id)
            )
        )
        .scalars()
        .all()
    }

    written: list[ArticleSource] = []
    seen_sources: set[uuid.UUID] = set()
    for report, source in rows:
        url = report.canonical_url or report.source_url
        key = (source.id, url)
        if key in existing:
            written.append(existing[key])
            seen_sources.add(source.id)
            continue
        # One attribution row per source (the unique key is article+source); the
        # first report from that source supplies the link.
        if source.id in seen_sources:
            continue
        seen_sources.add(source.id)
        row = ArticleSource(
            article_id=article.id,
            source_id=source.id,
            source_report_id=report.id,
            url=url,
            source_name=source.name,
            is_independent=True,
        )
        session.add(row)
        written.append(row)
    await session.flush()
    return written


def _changed_enough(previous: str | None, current: str) -> bool:
    if previous is None:
        return True
    old = set(normalize_text(previous).split())
    new = set(normalize_text(current).split())
    if not old and not new:
        return False
    union = old | new
    if not union:
        return False
    return (len(union) - len(old & new)) / len(union) >= _MIN_BODY_CHANGE


async def generate_article_for_event(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    audience_country: str | None = None,
    reason: str = "initial",
) -> GenerationOutcome:
    """Generate or update the article for one event, validating before publish."""
    event = await session.get(Event, event_id)
    if event is None:
        raise ValueError(f"event {event_id} no longer exists")

    if not ai_provider_configured():
        return GenerationOutcome(
            article_id=None,
            published=False,
            reason="no AI provider configured; not publishing",
        )

    # Confidence gate (section 12): never publish below the floor.
    if event.confidence_score < settings.min_confidence_to_publish:
        return GenerationOutcome(
            article_id=None,
            published=False,
            reason=(
                f"confidence {event.confidence_score:.1f} below publish floor "
                f"{settings.min_confidence_to_publish}"
            ),
        )
    if event.conflict_count > 0:
        return GenerationOutcome(
            article_id=None,
            published=False,
            reason=f"{event.conflict_count} unresolved conflict(s); not publishing",
        )

    await recompute_importance(session, event)
    # Importance gate (section 13): a confirmed but trivial event is not news.
    if event.importance_score < settings.min_importance_to_publish:
        return GenerationOutcome(
            article_id=None,
            published=False,
            reason=(
                f"importance {event.importance_score:.1f} below publish floor "
                f"{settings.min_importance_to_publish}"
            ),
        )

    evidence = await build_evidence_package(session, event, audience_country=audience_country)
    if not evidence["confirmed_facts"] and not evidence["single_source_facts"]:
        return GenerationOutcome(article_id=None, published=False, reason="no facts to write from")

    provider = get_ai_provider()
    draft = await provider.draft_article(evidence=evidence, audience_country=audience_country)

    source_texts = await _source_texts(session, event_id)
    validation = validate_draft(draft, evidence, source_texts=source_texts)

    # Layer 5: AI fact-check, only when the deterministic gate already passed.
    if validation.ok and settings.ai_fact_check_enabled and provider.name != "offline":
        try:
            unsupported = await provider.fact_check(evidence=evidence, draft=draft)
        except Exception:  # noqa: BLE001 - a fact-check outage must not publish
            unsupported = ["fact-check pass unavailable"]
        if unsupported:
            validation = ValidationResult(
                ok=False,
                failures=["fact-check flagged unsupported claims: " + "; ".join(unsupported)],
                warnings=validation.warnings,
            )

    event.status = EventStatus.EDITORIAL_REVIEW
    await session.flush()

    if not validation.ok:
        logger.warning("article for event %s rejected: %s", event.id, validation.reason)
        return GenerationOutcome(
            article_id=None,
            published=False,
            reason=f"validation failed: {validation.reason}",
            validation=validation,
        )

    article = (
        await session.execute(_article_stmt(event.id, audience_country))
    ).scalar_one_or_none()
    created = article is None
    if article is None:
        article = Article(
            event_id=event.id,
            is_global=audience_country is None,
            locale_country=audience_country,
            slug=await _unique_slug(session, _slugify(draft.headline)),
            headline=draft.headline,
            body=draft.body,
            current_version=1,
        )
        session.add(article)
        await session.flush()
    elif not _changed_enough(article.body, draft.body):
        # Nothing material changed; keep one article and one version.
        await _upsert_attribution(session, article, event.id)
        article.is_published = True
        article.published_at = article.published_at or datetime.now(UTC)
        event.status = EventStatus.PUBLISHED
        await session.flush()
        return GenerationOutcome(
            article_id=article.id,
            published=True,
            reason="no material change; kept existing version",
            version=article.current_version,
            validation=validation,
        )
    else:
        article.current_version += 1

    article.headline = draft.headline
    article.subtitle = draft.subtitle
    article.body = draft.body
    article.key_points = json.dumps(draft.key_points)
    article.timeline = json.dumps(draft.timeline)
    article.category = draft.category or event.event_type
    article.location = draft.location or event.city or event.country
    article.seo_title = draft.seo_title
    article.seo_description = draft.seo_description
    article.confidence_score = event.confidence_score
    article.importance_score = event.importance_score
    article.is_published = True
    article.published_at = article.published_at or datetime.now(UTC)
    await session.flush()

    session.add(
        ArticleVersion(
            article_id=article.id,
            version=article.current_version,
            content=article.body,
            reason=reason,
        )
    )
    await _upsert_attribution(session, article, event.id)

    event.status = EventStatus.PUBLISHED
    await session.flush()

    logger.info(
        "article %s v%d %s for event %s (confidence %.1f, importance %.1f)",
        article.id,
        article.current_version,
        "created" if created else "updated",
        event.id,
        article.confidence_score,
        article.importance_score,
    )
    return GenerationOutcome(
        article_id=article.id,
        published=True,
        reason="published" if created else "updated",
        version=article.current_version,
        validation=validation,
    )


async def _localized_countries(session: AsyncSession, event_id: uuid.UUID) -> list[str]:
    """Countries a supported audience should get a localized edition for.

    Bounded on purpose: localize for the country the event is about when that
    country is supported, otherwise for the countries whose *configured local
    source* reported it (spec section 4). No LLM picks the country or the
    source; both come from the ``country_sources`` data. The local feed still
    shows an event about a country through the global article, so this only adds
    the country-angle edition rather than gating local coverage on it.
    """
    event = await session.get(Event, event_id)
    if event is None:
        return []

    candidate = await session.get(Country, event.country) if event.country else None
    if candidate is not None and candidate.is_supported:
        return [event.country]

    rows = (
        await session.execute(
            select(CountrySource.country_code)
            .join(SourceReport, SourceReport.source_id == CountrySource.source_id)
            .where(
                CountrySource.enabled.is_(True),
                SourceReport.id.in_(
                    select(EventReport.source_report_id).where(EventReport.event_id == event_id)
                ),
            )
            .distinct()
        )
    ).scalars()
    return sorted(rows.all())


async def generate_localized_articles(
    session: AsyncSession, event_id: uuid.UUID, *, reason: str = "localized"
) -> dict[str, GenerationOutcome]:
    """Generate a localized edition of an event for each relevant country.

    Each locale reuses the same verified evidence package (spec section 10), so
    the localized writer can only re-angle facts that were already confirmed —
    it cannot introduce local claims the evidence does not support.
    """
    outcomes: dict[str, GenerationOutcome] = {}
    for country in await _localized_countries(session, event_id):
        outcomes[country] = await generate_article_for_event(
            session, event_id, audience_country=country, reason=reason
        )
    return outcomes


async def record_image_reference(
    session: AsyncSession,
    article_id: uuid.UUID,
    *,
    provider: str,
    prompt_hash: str,
    generation_id: str | None = None,
    ephemeral_url: str | None = None,
    expires_at: datetime | None = None,
) -> ArticleImage:
    """Record image *metadata* only (spec section 20).

    Image bytes are never stored on our infrastructure; at most a temporary
    provider URL is referenced under the provider's terms.
    """
    image = ArticleImage(
        article_id=article_id,
        image_provider=provider,
        generation_id=generation_id,
        prompt_hash=prompt_hash,
        ephemeral_url=ephemeral_url,
        expires_at=expires_at,
    )
    session.add(image)
    await session.flush()
    return image
