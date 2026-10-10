"""Phase 8 accuracy, duplicate and integration tests against real PostgreSQL.

These are the spec's "accuracy testing / duplicate testing" requirements
(section 36) expressed as an executable regression suite:

* A synthetic corpus of *one real-world event* reported by many sources in
  different words must collapse to a single event and a single article, and the
  independence count must reflect reporting chains, not website count.
* A hall of fabricated drafts (wrong numbers, invented officials, invented URLs,
  copied prose) must all be rejected.
* The security/cost additions are exercised through the real API and DB.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.main import app
from app.models.article import Article, ArticleSource, ArticleVersion
from app.models.enums import SourceReportStatus, SourceType
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
from tests.conftest import database_available

# --- Shared fixtures ----------------------------------------------------------


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

    async def _make(
        *,
        name: str,
        country: str | None = None,
        lineage_root_id: uuid.UUID | None = None,
        reliability_score: float = 85.0,
    ) -> Source:
        src = Source(
            slug=f"p8-{uuid.uuid4().hex[:8]}",
            name=name,
            country=country,
            type=SourceType.rss,
            website_url="https://example.com",
            rss_url="https://example.com/rss.xml",
            language="en",
            poll_interval_seconds=300,
            lineage_root_id=lineage_root_id,
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
    async def _make(
        source: Source,
        *,
        title: str,
        description: str = "",
        published_at: datetime | None = None,
    ) -> SourceReport:
        now = datetime.now(UTC)
        token = uuid.uuid4().hex
        report = SourceReport(
            source_id=source.id,
            source_url=f"https://example.com/{token}",
            canonical_url=f"https://example.com/{token}",
            canonical_url_hash=token + token,
            title=title,
            description=description or None,
            published_at=published_at or now,
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
    from app.core.config import settings as s

    monkeypatch.setattr(s, "ai_provider", "offline", raising=False)
    yield
    set_ai_provider(None)


class _ControlledProvider(AIProvider):
    """A real provider whose draft the test controls, to probe validation."""

    name = "controlled"

    def __init__(self, *, body: str, headline: str, unsupported: list[str] | None = None) -> None:
        self._body = body
        self._headline = headline
        self._unsupported = unsupported or []

    async def draft_article(self, *, evidence, audience_country=None) -> ArticleDraft:
        return ArticleDraft(headline=self._headline, body=self._body)

    async def fact_check(self, *, evidence, draft) -> list[str]:
        return list(self._unsupported)


# --- Accuracy corpus: many sources, one event, one article --------------------


# Five differently-worded reports of the same earthquake, all carrying the two
# identifying numbers (magnitude 6.8, 12 deaths). Reuters and the two "wire
# republishers" share a lineage root, so they are ONE independent chain; the
# meteorological agency and NHK are each their own chain. Expectation: one event,
# THREE independent chains from five websites, one article. The wording differs
# enough that the offline embeddings must lean on the shared numbers to cluster
# them, which is exactly the behaviour under test.
_CORPUS = [
    ("Reuters", "Earthquake strikes Japan killing 12", "A magnitude 6.8 earthquake struck Japan."),
    (
        "Daily Wire Republisher",
        "Earthquake strikes Japan killing 12 people",
        "A magnitude 6.8 earthquake struck Japan, killing 12 people.",
    ),
    (
        "Metro Herald",
        "Strong earthquake strikes Japan, killing 12",
        "A magnitude 6.8 earthquake struck Japan, killing 12 people.",
    ),
    (
        "Japan Meteorological Agency",
        "Magnitude 6.8 earthquake strikes Japan killing 12",
        "A magnitude 6.8 earthquake struck Japan, killing 12 people.",
    ),
    (
        "NHK",
        "Powerful earthquake strikes Japan killing 12",
        "A magnitude 6.8 earthquake struck Japan, killing 12 people.",
    ),
]


async def _build_corpus_event(db_session, make_source, make_report):
    root = await make_source(name="Reuters")
    sources = [
        root,
        await make_source(name="Daily Wire Republisher", lineage_root_id=root.id),
        await make_source(name="Metro Herald", lineage_root_id=root.id),
        await make_source(name="Japan Meteorological Agency"),
        await make_source(name="NHK"),
    ]
    event = None
    for source, (_, title, description) in zip(sources, _CORPUS, strict=True):
        report = await make_report(source, title=title, description=description)
        event, _created = await cluster_report(db_session, report.id)
    return event


async def test_corpus_collapses_to_one_event(db_session, clean_events, make_source, make_report):
    event = await _build_corpus_event(db_session, make_source, make_report)

    event_count = (await db_session.execute(select(func.count()).select_from(Event))).scalar_one()
    assert event_count == 1, "five reports of one quake must be one event"

    member_count = (
        await db_session.execute(
            select(func.count()).select_from(EventReport).where(EventReport.event_id == event.id)
        )
    ).scalar_one()
    assert member_count == 5


async def test_corpus_counts_reporting_chains_not_websites(
    db_session, clean_events, make_source, make_report
):
    event = await _build_corpus_event(db_session, make_source, make_report)
    verified = await verify_event(db_session, event.id)

    # Three independent chains (Reuters + its two republishers collapse to one),
    # not five websites.
    assert verified.independent_source_count == 3
    assert verified.report_count == 5


async def test_corpus_produces_exactly_one_published_article(
    db_session, clean_articles, clean_events, make_source, make_report
):
    event = await _build_corpus_event(db_session, make_source, make_report)
    verified = await verify_event(db_session, event.id)

    outcome = await generate_article_for_event(db_session, verified.id)
    await db_session.commit()

    assert outcome.published is True
    article_count = (
        await db_session.execute(select(func.count()).select_from(Article))
    ).scalar_one()
    assert article_count == 1, "one event must yield one editorial article"


# --- Hallucination hall of fame: fabricated drafts must be rejected -----------


def _evidence_fixture() -> dict:
    return {
        "event": {"title": "Earthquake strikes Japan", "event_type": "earthquake"},
        "confirmed_facts": [
            {"fact": "Earthquake occurred", "sources": ["s1", "s2"]},
            {"fact": "Magnitude was 6.8", "sources": ["s1", "s2"]},
            {"fact": "12 people were killed", "sources": ["s1", "s2"]},
        ],
        "single_source_facts": [],
        "conflicting_claims": [],
    }


@pytest.mark.parametrize(
    "body",
    [
        # invented death toll that is not in the evidence
        "The earthquake killed 48 people across the region.",
        # invented magnitude
        "The quake registered magnitude 9.1, the strongest in years.",
        # invented official
        "Prime Minister Kenji Watanabe declared a national emergency.",
        # invented URL
        "Full coverage at https://not-a-real-source.example/quake",
    ],
)
async def test_fabricated_draft_is_not_published(body):
    """Layer 6: a deterministic validator rejects unsupported claims."""
    from app.services.article_validation import validate_draft

    draft = ArticleDraft(headline="Earthquake strikes Japan", body=body)
    result = validate_draft(draft, _evidence_fixture())
    assert result.ok is False


async def test_supported_draft_passes_validation():
    from app.services.article_validation import validate_draft

    body = (
        "An earthquake occurred in Japan. The magnitude was 6.8. "
        "Authorities reported that 12 people were killed."
    )
    draft = ArticleDraft(headline="Earthquake strikes Japan", body=body)
    result = validate_draft(draft, _evidence_fixture())
    assert result.ok is True, result.reason


# --- Confidence / conflict behaviour through the real pipeline ----------------


async def test_conflicting_casualty_figures_are_flagged(
    db_session, clean_articles, clean_events, make_source, make_report
):
    """Spec section 24: differing casualty figures must be detected, not averaged.

    The event may not reach the confidence floor with an unresolved conflict, so
    either the conflict gate or the confidence gate must stop publication — both
    are correct "do not publish as fact" outcomes.
    """
    sources = [await make_source(name=f"Source {i}") for i in range(3)]
    figures = ["5 people were killed", "12 people were killed", "7 people were killed"]
    event = None
    for source, figure in zip(sources, figures, strict=True):
        report = await make_report(
            source,
            title="Earthquake strikes Japan killing 12",
            description=f"A magnitude 6.8 earthquake struck Japan. {figure}.",
        )
        event, _ = await cluster_report(db_session, report.id)

    verified = await verify_event(db_session, event.id)
    assert verified.conflict_count > 0, "conflicting casualty counts must be detected"

    outcome = await generate_article_for_event(db_session, verified.id)
    assert outcome.published is False


# --- Cost control end to end --------------------------------------------------


async def test_exhausted_budget_defers_generation(
    db_session, clean_articles, clean_events, make_source, make_report, monkeypatch
):
    """An exhausted AI budget defers rather than publishing or crashing."""
    from app.services import cost_control

    event = await _build_corpus_event(db_session, make_source, make_report)
    verified = await verify_event(db_session, event.id)

    monkeypatch.setattr(settings, "ai_budget_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ai_max_requests_per_hour", 1, raising=False)
    # Lift the earlier gates so the budget check is what stops generation.
    monkeypatch.setattr(settings, "min_confidence_to_publish", 0, raising=False)
    monkeypatch.setattr(settings, "min_importance_to_publish", 0, raising=False)
    cost_control.set_counter(None)
    try:
        await cost_control.reserve_ai_call()  # spend the single allowed call
        outcome = await generate_article_for_event(db_session, verified.id)
        assert outcome.published is False
        assert "budget" in outcome.reason.lower()
    finally:
        cost_control.set_counter(None)


# --- API integration: headers, budget in metrics, admin auth logging ----------


@pytest.fixture
async def client():
    if not await database_available():
        pytest.skip("test database not available (see conftest for setup)")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_security_headers_present_on_responses(client):
    resp = await client.get("/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in resp.headers
    assert resp.headers["Referrer-Policy"] == "no-referrer"


async def test_metrics_snapshot_includes_cost_budget(client):
    resp = await client.get("/admin/metrics", headers={"X-Admin-Token": settings.admin_api_token})
    assert resp.status_code == 200
    cost = resp.json()["cost"]
    assert {
        "ai_calls_used_this_hour",
        "ai_calls_limit_per_hour",
        "ai_budget_remaining",
        "ai_budget_exhausted",
    } <= cost.keys()


async def test_admin_auth_failure_is_logged_without_the_token(client, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="security"):
        resp = await client.get("/admin/metrics", headers={"X-Admin-Token": "wrong-token"})
    assert resp.status_code == 401
    assert "admin auth failure" in caplog.text
    assert "wrong-token" not in caplog.text
