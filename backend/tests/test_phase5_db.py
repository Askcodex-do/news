"""Phase 5 integration tests against a real PostgreSQL.

No mocks of the code under test. These drive the real ranking query, the real
localized-generation path and the real partial unique indexes in the schema,
writing rows to the isolated ``news_test`` database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.models.article import Article
from app.models.country import Country
from app.models.enums import EventStatus, SourceReportStatus, SourceType
from app.models.event import (
    Event,
    EventConflict,
    EventFact,
    EventFactSource,
    EventReport,
    EventUpdate,
)
from app.models.source import CountrySource, Source
from app.models.source_report import SourceReport
from app.services.ai import AIProvider, ArticleDraft, set_ai_provider
from app.services.article_generation import (
    generate_article_for_event,
    generate_localized_articles,
)
from app.services.clustering import cluster_report
from app.services.ranking import load_ranked_feed
from app.services.verification import verify_event


class _StubProvider(AIProvider):
    """A real provider whose output the test controls (not a mock)."""

    name = "stub"

    def __init__(self, body_for: str | None = None) -> None:
        # When set, a localized draft (audience_country present) uses this body,
        # so a test can inject a fabricated local claim and assert it is blocked.
        self._body_for = body_for

    async def draft_article(self, *, evidence, audience_country=None) -> ArticleDraft:
        if audience_country and self._body_for is not None:
            body = self._body_for
        else:
            facts = [str(f.get("fact", "")) for f in evidence.get("confirmed_facts", [])]
            body = " ".join(facts)
        return ArticleDraft(
            headline=evidence["event"]["title"],
            body=body,
            category=evidence["event"].get("event_type"),
            location=evidence["event"].get("location"),
        )

    async def fact_check(self, *, evidence, draft) -> list[str]:
        return []


@pytest.fixture
async def clean_events(db_session, make_source):
    async def _clean() -> None:
        await db_session.execute(delete(EventUpdate))
        await db_session.execute(delete(EventConflict))
        await db_session.execute(delete(EventReport))
        await db_session.execute(delete(EventFactSource))
        await db_session.execute(delete(EventFact))
        await db_session.execute(delete(Event))
        await db_session.commit()

    await _clean()
    yield
    await _clean()


@pytest.fixture
async def clean_articles(db_session, clean_events):
    from app.models.article import ArticleSource, ArticleVersion

    async def _clean() -> None:
        await db_session.execute(delete(ArticleVersion))
        await db_session.execute(delete(ArticleSource))
        await db_session.execute(delete(Article))
        await db_session.commit()

    await _clean()
    yield
    await _clean()


@pytest.fixture
async def make_source(db_session):
    created: list[uuid.UUID] = []

    async def _make(*, name: str, reliability_score: float = 85.0) -> Source:
        src = Source(
            slug=f"p5-{uuid.uuid4().hex[:8]}",
            name=name,
            type=SourceType.rss,
            website_url="https://example.com",
            rss_url="https://example.com/rss.xml",
            language="en",
            poll_interval_seconds=300,
            reliability_score=reliability_score,
        )
        db_session.add(src)
        await db_session.flush()
        created.append(src.id)
        return src

    yield _make
    for source_id in created:
        await db_session.execute(delete(SourceReport).where(SourceReport.source_id == source_id))
    for source_id in reversed(created):
        await db_session.execute(delete(Source).where(Source.id == source_id))
    await db_session.commit()


@pytest.fixture
async def make_report(db_session):
    async def _make(source: Source, *, title: str, description: str = "") -> SourceReport:
        now = datetime.now(UTC)
        token = uuid.uuid4().hex
        report = SourceReport(
            source_id=source.id,
            source_url=f"https://example.com/{token}",
            canonical_url=f"https://example.com/{token}",
            canonical_url_hash=token + token,
            title=title,
            description=description or None,
            published_at=now,
            retrieved_at=now,
            content_hash=token + token,
            status=SourceReportStatus.NEW,
        )
        db_session.add(report)
        await db_session.flush()
        return report

    return _make


@pytest.fixture
async def country_edition(db_session, make_source):
    """Configure a supported country -> local source mapping (spec section 4).

    Reference country rows are left in place (idempotent upsert); only the
    mapping is removed, before ``make_source`` removes the source it points at.
    """
    codes: list[str] = []

    async def _configure(code: str, *, source: Source) -> str:
        country = await db_session.get(Country, code)
        if country is None:
            db_session.add(Country(code=code, name=code, is_supported=True))
        else:
            country.is_supported = True
        db_session.add(
            CountrySource(country_code=code, source_id=source.id, priority=90, enabled=True)
        )
        await db_session.flush()
        codes.append(code)
        return code

    yield _configure
    for code in codes:
        await db_session.execute(delete(CountrySource).where(CountrySource.country_code == code))
    await db_session.commit()


@pytest.fixture(autouse=True)
def offline_ai(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ai_provider", "offline", raising=False)
    yield
    set_ai_provider(None)


_HEADLINE_A = "Earthquake strikes Japan killing 12"
_HEADLINE_B = "Earthquake strikes Japan killing 12 people"
_DESCRIPTION_A = "A magnitude 6.8 earthquake struck Japan."
_DESCRIPTION_B = "Authorities said the magnitude 6.8 quake killed 12 people."


async def _build_verified_event(db_session, make_source, make_report, *, country="JP"):
    sources = [
        await make_source(name="Reuters"),
        await make_source(name="Japan Meteorological Agency"),
    ]
    report_ids = []
    for source, headline, description in zip(
        sources,
        (_HEADLINE_A, _HEADLINE_B),
        (_DESCRIPTION_A, _DESCRIPTION_B),
        strict=True,
    ):
        report = await make_report(source, title=headline, description=description)
        report_ids.append(report.id)

    event = None
    for report_id in report_ids:
        event, _created = await cluster_report(db_session, report_id)
    event.country = country
    await db_session.flush()
    return await verify_event(db_session, event.id)


async def test_localized_generation_makes_one_article_per_locale(
    db_session, clean_articles, clean_events, make_source, make_report, country_edition
):
    local_source = await make_source(name="NHK")
    await country_edition("JP", source=local_source)
    event = await _build_verified_event(db_session, make_source, make_report)
    assert event.status == EventStatus.VERIFIED

    global_outcome = await generate_article_for_event(db_session, event.id)
    assert global_outcome.published, global_outcome.reason

    outcomes = await generate_localized_articles(db_session, event.id)
    assert "JP" in outcomes
    assert outcomes["JP"].published, outcomes["JP"].reason

    articles = (
        (await db_session.execute(select(Article).where(Article.event_id == event.id)))
        .scalars()
        .all()
    )
    assert len(articles) == 2
    globals_ = [a for a in articles if a.is_global]
    locals_ = [a for a in articles if not a.is_global]
    assert len(globals_) == 1 and len(locals_) == 1
    assert globals_[0].locale_country is None
    assert locals_[0].locale_country == "JP"


async def test_localized_generation_is_idempotent(
    db_session, clean_articles, clean_events, make_source, make_report, country_edition
):
    local_source = await make_source(name="NHK")
    await country_edition("JP", source=local_source)
    event = await _build_verified_event(db_session, make_source, make_report)
    await generate_article_for_event(db_session, event.id)

    first = await generate_localized_articles(db_session, event.id)
    second = await generate_localized_articles(db_session, event.id)
    assert first["JP"].article_id == second["JP"].article_id

    count = len(
        (
            await db_session.execute(
                select(Article).where(Article.event_id == event.id, Article.is_global.is_(False))
            )
        )
        .scalars()
        .all()
    )
    assert count == 1


async def test_localized_fabrication_is_not_published(
    db_session, clean_articles, clean_events, make_source, make_report, country_edition
):
    """A localized draft that invents a fact is rejected, not published.

    The fabricated number (9999) is not in the evidence, so the deterministic
    validator must block it — localization cannot introduce local claims.
    """
    local_source = await make_source(name="NHK")
    await country_edition("JP", source=local_source)
    event = await _build_verified_event(db_session, make_source, make_report)
    await generate_article_for_event(db_session, event.id)

    set_ai_provider(_StubProvider(body_for="The earthquake killed 9999 people in Tokyo."))
    outcomes = await generate_localized_articles(db_session, event.id)
    assert outcomes["JP"].published is False
    assert "validation failed" in outcomes["JP"].reason

    localized = (
        (
            await db_session.execute(
                select(Article).where(Article.event_id == event.id, Article.is_global.is_(False))
            )
        )
        .scalars()
        .all()
    )
    assert localized == []


async def test_local_feed_includes_localized_and_country_events(
    db_session, clean_articles, clean_events, make_source, make_report, country_edition
):
    local_source = await make_source(name="NHK")
    await country_edition("JP", source=local_source)
    event = await _build_verified_event(db_session, make_source, make_report)
    await generate_article_for_event(db_session, event.id)
    await generate_localized_articles(db_session, event.id)

    global_articles, local_articles, local_source_name = await load_ranked_feed(
        db_session, country_code="JP"
    )
    assert local_source_name == "NHK"
    # The global feed carries the global article; the local feed carries the
    # Japan-angle edition of the same event (spec section 21).
    assert len(global_articles) == 1
    assert global_articles[0].is_global
    assert len(local_articles) == 1
    assert local_articles[0].locale_country == "JP"


async def test_feed_for_unmapped_country_has_no_local_edition(
    db_session, clean_articles, clean_events, make_source, make_report
):
    event = await _build_verified_event(db_session, make_source, make_report)
    await generate_article_for_event(db_session, event.id)

    global_articles, local_articles, local_source_name = await load_ranked_feed(
        db_session, country_code="ZZ"
    )
    assert local_source_name is None
    assert local_articles == []
    assert len(global_articles) == 1


async def test_schema_allows_one_global_and_one_per_locale(
    db_session, clean_articles, clean_events, make_source, make_report
):
    event = await _build_verified_event(db_session, make_source, make_report)

    db_session.add(
        Article(
            event_id=event.id,
            is_global=True,
            slug=f"g-{uuid.uuid4().hex[:8]}",
            headline="Global",
            body="body",
        )
    )
    db_session.add(
        Article(
            event_id=event.id,
            is_global=False,
            locale_country="JP",
            slug=f"l-{uuid.uuid4().hex[:8]}",
            headline="Japan",
            body="body",
        )
    )
    await db_session.flush()

    # A second global article for the same event violates the partial index.
    db_session.add(
        Article(
            event_id=event.id,
            is_global=True,
            slug=f"g2-{uuid.uuid4().hex[:8]}",
            headline="Duplicate",
            body="body",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
