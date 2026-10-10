"""Phase 4 integration tests against a real PostgreSQL.

No mocks of the code under test. The AI writer is a real ``AIProvider``
subclass defined here, so ``generate_article_for_event`` runs its actual
evidence-building, validation, versioning and attribution code paths and writes
real rows to the isolated ``news_test`` database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from app.models.article import Article, ArticleSource, ArticleVersion
from app.models.enums import EventStatus, SourceReportStatus, SourceType
from app.models.event import (
    Event,
    EventConflict,
    EventFact,
    EventFactSource,
    EventReport,
    EventUpdate,
)
from app.models.source import Source
from app.models.source_report import SourceReport
from app.services.ai import AIProvider, ArticleDraft, set_ai_provider
from app.services.article_generation import generate_article_for_event
from app.services.clustering import cluster_report
from app.services.verification import verify_event


class _StubProvider(AIProvider):
    """A real provider whose output the test controls, to drive the pipeline.

    Deliberately not a mock: it satisfies the provider interface and the
    production code calls it normally. It lets a test inject either a valid
    synthesis or a hallucinated one and assert what the pipeline does.
    """

    name = "stub"

    def __init__(
        self, body: str, *, headline: str | None = None, unsupported: list[str] | None = None
    ) -> None:
        self._body = body
        self._headline = headline
        self._unsupported = unsupported or []

    async def draft_article(self, *, evidence, audience_country=None) -> ArticleDraft:
        return ArticleDraft(
            headline=self._headline or evidence["event"]["title"],
            body=self._body,
            category=evidence["event"].get("event_type"),
            location=evidence["event"].get("location"),
        )

    async def fact_check(self, *, evidence, draft) -> list[str]:
        return list(self._unsupported)


@pytest.fixture
async def clean_events(db_session, make_source):
    """Remove events and their dependents before and after each test.

    Depends on ``make_source`` so this teardown (which deletes events) runs
    before ``make_source`` teardown deletes the reports those events point at.
    """

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
    """Remove articles before the events they belong to are removed.

    Depends on ``clean_events`` so this teardown runs first (articles are
    children of events).
    """

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
            slug=f"art-{uuid.uuid4().hex[:8]}",
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


@pytest.fixture(autouse=True)
def offline_ai(monkeypatch):
    """Force the offline provider so tests never call a paid API."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "ai_provider", "offline", raising=False)
    yield
    set_ai_provider(None)


# Two phrasings of the same quake. They are worded differently enough not to
# count as near-duplicates (so they are two independent chains) yet close enough
# to cluster into one event — which is exactly the multi-source case Phase 4
# generates from.
_HEADLINE_A = "Earthquake strikes Japan killing 12"
_HEADLINE_B = "Earthquake strikes Japan killing 12 people"
_DESCRIPTION_A = "A magnitude 6.8 earthquake struck Japan."
_DESCRIPTION_B = "Authorities said the magnitude 6.8 quake killed 12 people."


async def _build_verified_event(db_session, make_source, make_report):
    """Two independent sources reporting the same quake; returned VERIFIED.

    One source is an official/agency feed, which is what lifts confidence over
    the publish floor. Only the reports created here are clustered, so a
    leftover report from another test cannot contaminate the event.
    """
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
    return await verify_event(db_session, event.id)


async def test_generation_publishes_validated_article(
    db_session, clean_articles, clean_events, make_source, make_report
):
    event = await _build_verified_event(db_session, make_source, make_report)
    assert event.status == EventStatus.VERIFIED

    outcome = await generate_article_for_event(db_session, event.id)
    assert outcome.published, outcome.reason
    assert outcome.article_id is not None

    article = await db_session.get(Article, outcome.article_id)
    assert article.is_published
    assert article.published_at is not None
    assert article.current_version == 1
    assert article.confidence_score == event.confidence_score
    assert article.importance_score > 0

    versions = (
        (
            await db_session.execute(
                select(ArticleVersion).where(ArticleVersion.article_id == article.id)
            )
        )
        .scalars()
        .all()
    )
    assert [v.version for v in versions] == [1]

    attribution = (
        (
            await db_session.execute(
                select(ArticleSource).where(ArticleSource.article_id == article.id)
            )
        )
        .scalars()
        .all()
    )
    names = {row.source_name for row in attribution}
    assert {"Reuters", "Japan Meteorological Agency"} <= names
    assert all(row.url for row in attribution)


async def test_generation_rejects_hallucinated_draft(
    db_session, clean_articles, clean_events, make_source, make_report
):
    event = await _build_verified_event(db_session, make_source, make_report)
    # The writer invents a death toll that is in no source report.
    set_ai_provider(
        _StubProvider(
            body=(
                "An earthquake struck Japan. Reports said 4800 people were killed, "
                "a figure that appears in no source and cannot be supported by the "
                "evidence package assembled for this event."
            )
        )
    )

    outcome = await generate_article_for_event(db_session, event.id)
    assert not outcome.published
    assert "validation failed" in outcome.reason
    assert any("numbers not in evidence" in f for f in outcome.validation.failures)

    # Nothing was published, and the event is waiting in editorial review.
    assert (
        await db_session.execute(select(Article).where(Article.event_id == event.id))
    ).scalar_one_or_none() is None
    refreshed = await db_session.get(Event, event.id)
    assert refreshed.status == EventStatus.EDITORIAL_REVIEW
    assert refreshed.status != EventStatus.PUBLISHED


async def test_generation_rejected_by_ai_fact_check(
    db_session, clean_articles, clean_events, make_source, make_report
):
    event = await _build_verified_event(db_session, make_source, make_report)
    set_ai_provider(
        _StubProvider(
            body=(
                "An earthquake struck Japan and 12 people were killed. Officials "
                "continue to assess the situation across the affected region as "
                "rescue teams work through the aftermath of the magnitude 6.8 quake."
            ),
            unsupported=["claim that a tsunami warning was issued"],
        )
    )
    outcome = await generate_article_for_event(db_session, event.id)
    assert not outcome.published
    assert any("fact-check flagged" in f for f in outcome.validation.failures)


async def test_regeneration_appends_version_and_reuses_one_article(
    db_session, clean_articles, clean_events, make_source, make_report
):
    event = await _build_verified_event(db_session, make_source, make_report)
    first = await generate_article_for_event(db_session, event.id, reason="initial")
    assert first.published

    # A materially different update to the same event.
    set_ai_provider(
        _StubProvider(
            body=(
                "An earthquake struck Japan and 12 people were killed. Rescue teams "
                "have now reached the worst-affected districts and officials say the "
                "magnitude 6.8 quake damaged roads and buildings across the region."
            )
        )
    )
    second = await generate_article_for_event(db_session, event.id, reason="update")
    assert second.published
    assert second.article_id == first.article_id
    assert second.version == 2

    articles = (
        (await db_session.execute(select(Article).where(Article.event_id == event.id)))
        .scalars()
        .all()
    )
    assert len(articles) == 1
    versions = (
        (
            await db_session.execute(
                select(ArticleVersion)
                .where(ArticleVersion.article_id == first.article_id)
                .order_by(ArticleVersion.version)
            )
        )
        .scalars()
        .all()
    )
    assert [v.version for v in versions] == [1, 2]
    assert versions[1].reason == "update"


async def test_generation_skips_without_ai_provider(
    db_session, clean_articles, clean_events, make_source, make_report, monkeypatch
):
    from app.core.config import settings

    event = await _build_verified_event(db_session, make_source, make_report)
    monkeypatch.setattr(settings, "ai_provider", "openai", raising=False)
    monkeypatch.setattr(settings, "ai_api_key", "", raising=False)

    outcome = await generate_article_for_event(db_session, event.id)
    assert not outcome.published
    assert "no AI provider configured" in outcome.reason


async def test_article_detail_endpoint_decodes_json_list_columns(
    db_session, clean_articles, clean_events, make_source, make_report, client
):
    """The detail endpoint must render a generated article, not 500.

    ``key_points`` and ``timeline`` are stored as JSON text but the response
    schema exposes lists; validating the raw string used to raise and 500 the
    endpoint that renders a published article and its source attribution.
    """
    event = await _build_verified_event(db_session, make_source, make_report)
    outcome = await generate_article_for_event(db_session, event.id)
    assert outcome.published, outcome.reason
    article = await db_session.get(Article, outcome.article_id)
    await db_session.commit()

    resp = await client.get(f"/articles/{article.slug}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["headline"] == article.headline
    assert isinstance(body["key_points"], list)
    assert isinstance(body["timeline"], list)
    assert len(body["sources"]) >= 2
