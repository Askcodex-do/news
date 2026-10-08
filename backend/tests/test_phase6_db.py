"""Phase 6 integration tests against a real PostgreSQL.

No mocks of the code under test: a real ``ImageProvider`` subclass drives
``attach_image_to_article`` through its actual persistence path, and the schema
is asserted to hold only metadata — never image bytes (spec section 20).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.models.article import Article, ArticleImage
from app.models.enums import EventStatus, SourceReportStatus, SourceType
from app.models.event import (
    Event,
    EventConflict,
    EventFact,
    EventFactSource,
    EventReport,
    EventUpdate,
)
from app.models.job import ProcessingJob
from app.models.source import Source
from app.models.source_report import SourceReport
from app.services.images import (
    GeneratedImage,
    ImageProvider,
    attach_image_to_article,
    purge_expired_image_references,
    set_image_provider,
)
from app.services.queue import JobType
from app.services.scheduler import _enqueue_image_jobs


class _StubImageProvider(ImageProvider):
    """A real provider whose output the test controls (not a mock)."""

    name = "stub-image"

    def __init__(self, *, url: str | None = "https://cdn.example/img.png", raises: bool = False):
        self._url = url
        self._raises = raises
        self.calls = 0

    async def generate(self, *, prompt: str) -> GeneratedImage:
        self.calls += 1
        if self._raises:
            raise RuntimeError("provider exploded")
        return GeneratedImage(
            provider=self.name,
            prompt=prompt,
            generation_id="gen-x",
            ephemeral_url=self._url,
            expires_at=datetime.now(UTC) + timedelta(seconds=3600),
            # A URL-less provider result has nothing to reference or audit.
            content_hash="deadbeef" if self._url else None,
        )


@pytest.fixture(autouse=True)
def reset_image_provider():
    yield
    set_image_provider(None)


@pytest.fixture(autouse=True)
async def clean_image_jobs(db_session):
    """Remove GENERATE_IMAGE jobs so idempotency assertions are deterministic.

    Only this module enqueues image jobs, so clearing them keeps tests isolated
    without touching other job types.
    """

    async def _clean() -> None:
        await db_session.execute(
            delete(ProcessingJob).where(ProcessingJob.job_type == JobType.GENERATE_IMAGE.value)
        )
        await db_session.commit()

    await _clean()
    yield
    await _clean()


@pytest.fixture
async def clean_articles(db_session, clean_events):
    from app.models.article import ArticleSource, ArticleVersion

    async def _clean() -> None:
        await db_session.execute(delete(ArticleImage))
        await db_session.execute(delete(ArticleVersion))
        await db_session.execute(delete(ArticleSource))
        await db_session.execute(delete(Article))
        await db_session.commit()

    await _clean()
    yield
    await _clean()


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
async def make_source(db_session):
    created: list[uuid.UUID] = []

    async def _make(*, name: str) -> Source:
        src = Source(
            slug=f"p6-{uuid.uuid4().hex[:8]}",
            name=name,
            type=SourceType.rss,
            website_url="https://example.com",
            rss_url="https://example.com/rss.xml",
            language="en",
            poll_interval_seconds=300,
            reliability_score=85.0,
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


async def _published_article(db_session, make_source, make_report) -> Article:
    source = await make_source(name="Reuters")
    await make_report(
        source, title="Magnitude 6.8 earthquake strikes Japan", description="A quake hit Japan."
    )
    now = datetime.now(UTC)
    event = Event(
        title="Magnitude 6.8 earthquake strikes Japan",
        event_type="earthquake",
        country="JP",
        status=EventStatus.PUBLISHED,
        confidence_score=90.0,
        importance_score=70.0,
        first_detected_at=now,
        last_updated_at=now,
    )
    db_session.add(event)
    await db_session.flush()

    article = Article(
        event_id=event.id,
        is_global=True,
        slug=f"p6-{uuid.uuid4().hex[:8]}",
        headline="Magnitude 6.8 earthquake strikes Japan",
        body="A magnitude 6.8 earthquake struck Japan.",
        category="earthquake",
        location="Japan",
        is_published=True,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.flush()
    return article


# --- no byte storage (spec section 20) ---------------------------------------


def test_article_images_schema_has_no_byte_columns():
    columns = set(ArticleImage.__table__.columns.keys())
    assert "ephemeral_url" in columns
    for forbidden in ("data", "bytes", "image_data", "blob", "content", "b64_json", "file_path"):
        assert forbidden not in columns


# --- attach: metadata only ---------------------------------------------------


async def test_attach_records_metadata_only(
    db_session, clean_articles, clean_events, make_source, make_report
):
    article = await _published_article(db_session, make_source, make_report)
    set_image_provider(_StubImageProvider())

    image = await attach_image_to_article(db_session, article.id, event_type="earthquake")
    assert image is not None
    assert image.image_provider == "stub-image"
    assert image.ephemeral_url == "https://cdn.example/img.png"
    assert image.generation_id == "gen-x"
    assert len(image.prompt_hash) == 64  # sha-256 hex
    assert image.expires_at is not None

    # The prompt recorded in the hash is derived from the event, not a source.
    stored = (
        await db_session.execute(select(ArticleImage).where(ArticleImage.article_id == article.id))
    ).scalar_one()
    assert stored.prompt_hash == image.prompt_hash


async def test_attach_is_idempotent_per_article(
    db_session, clean_articles, clean_events, make_source, make_report
):
    article = await _published_article(db_session, make_source, make_report)
    provider = _StubImageProvider()
    set_image_provider(provider)

    first = await attach_image_to_article(db_session, article.id)
    second = await attach_image_to_article(db_session, article.id)
    assert first.id == second.id
    assert provider.calls == 1  # not regenerated

    count = len(
        (
            await db_session.execute(
                select(ArticleImage).where(ArticleImage.article_id == article.id)
            )
        )
        .scalars()
        .all()
    )
    assert count == 1


async def test_attach_returns_none_when_provider_fails(
    db_session, clean_articles, clean_events, make_source, make_report
):
    """A failing image provider must not raise — the article is unaffected."""
    article = await _published_article(db_session, make_source, make_report)
    set_image_provider(_StubImageProvider(raises=True))

    image = await attach_image_to_article(db_session, article.id)
    assert image is None

    # The article is still published; nothing about it changed.
    refreshed = await db_session.get(Article, article.id)
    assert refreshed.is_published
    count = len(
        (
            await db_session.execute(
                select(ArticleImage).where(ArticleImage.article_id == article.id)
            )
        )
        .scalars()
        .all()
    )
    assert count == 0


async def test_attach_stores_nothing_when_provider_has_no_output(
    db_session, clean_articles, clean_events, make_source, make_report
):
    article = await _published_article(db_session, make_source, make_report)
    set_image_provider(_StubImageProvider(url=None))
    image = await attach_image_to_article(db_session, article.id)
    assert image is None


# --- purge (spec section 20) -------------------------------------------------


async def test_purge_removes_only_expired_references(
    db_session, clean_articles, clean_events, make_source, make_report
):
    article = await _published_article(db_session, make_source, make_report)
    now = datetime.now(UTC)
    db_session.add(
        ArticleImage(
            article_id=article.id,
            image_provider="stub",
            prompt_hash="a" * 64,
            ephemeral_url="https://cdn.example/old.png",
            expires_at=now - timedelta(seconds=5),
        )
    )
    db_session.add(
        ArticleImage(
            article_id=article.id,
            image_provider="stub",
            prompt_hash="b" * 64,
            ephemeral_url="https://cdn.example/new.png",
            expires_at=now + timedelta(hours=1),
        )
    )
    await db_session.flush()

    removed = await purge_expired_image_references(db_session)
    assert removed == 1
    remaining = (
        (
            await db_session.execute(
                select(ArticleImage).where(ArticleImage.article_id == article.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(remaining) == 1
    assert remaining[0].ephemeral_url == "https://cdn.example/new.png"


# --- scheduler wiring --------------------------------------------------------


async def test_enqueue_image_jobs_skips_when_not_configured(
    db_session, clean_articles, clean_events, make_source, make_report, monkeypatch
):
    from app.services import scheduler

    article = await _published_article(db_session, make_source, make_report)
    monkeypatch.setattr(scheduler, "image_provider_configured", lambda: False)
    await _enqueue_image_jobs(db_session, article.event_id)
    await db_session.flush()

    jobs = (
        (
            await db_session.execute(
                select(ProcessingJob).where(ProcessingJob.job_type == JobType.GENERATE_IMAGE.value)
            )
        )
        .scalars()
        .all()
    )
    assert jobs == []


async def test_enqueue_image_jobs_is_idempotent(
    db_session, clean_articles, clean_events, make_source, make_report, monkeypatch
):
    from app.services import scheduler

    article = await _published_article(db_session, make_source, make_report)
    monkeypatch.setattr(scheduler, "image_provider_configured", lambda: True)

    await _enqueue_image_jobs(db_session, article.event_id)
    await _enqueue_image_jobs(db_session, article.event_id)  # duplicate call
    await db_session.flush()

    jobs = (
        (
            await db_session.execute(
                select(ProcessingJob).where(
                    ProcessingJob.job_type == JobType.GENERATE_IMAGE.value,
                    ProcessingJob.idempotency_key == f"{JobType.GENERATE_IMAGE.value}:{article.id}",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(jobs) == 1
