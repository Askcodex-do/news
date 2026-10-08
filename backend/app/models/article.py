from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class Article(Base, UUIDMixin, TimestampMixin):
    """An original, AI-synthesized article for one event.

    One event may have a global version plus one localized version per country
    (spec section 18). The partial unique indexes below allow that while still
    preventing duplicate global articles or duplicate versions for the same
    locale. A developing story keeps one article per locale and appends
    versions (spec section 22).
    """

    __tablename__ = "articles"
    __table_args__ = (
        Index(
            "uq_articles_event_global",
            "event_id",
            unique=True,
            postgresql_where=text("is_global"),
        ),
        Index(
            "uq_articles_event_locale",
            "event_id",
            "locale_country",
            unique=True,
            postgresql_where=text("NOT is_global"),
        ),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("events.id"), nullable=False
    )
    # Locale/country of this editorial version (spec section 18).
    locale_country: Mapped[str | None] = mapped_column(String(2), index=True)
    is_global: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    slug: Mapped[str] = mapped_column(String(240), unique=True, nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    subtitle: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    key_points: Mapped[list | None] = mapped_column(Text)  # JSON-encoded list
    timeline: Mapped[list | None] = mapped_column(Text)  # JSON-encoded list

    category: Mapped[str | None] = mapped_column(String(64), index=True)
    location: Mapped[str | None] = mapped_column(Text)
    seo_title: Mapped[str | None] = mapped_column(Text)
    seo_description: Mapped[str | None] = mapped_column(Text)

    confidence_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    importance_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    current_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, index=True, nullable=False)


class ArticleVersion(Base, UUIDMixin, TimestampMixin):
    """Immutable history of an article's revisions (spec section 22)."""

    __tablename__ = "article_versions"
    __table_args__ = (UniqueConstraint("article_id", "version", name="uq_article_version"),)

    article_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("articles.id"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)


class ArticleSource(Base, UUIDMixin, TimestampMixin):
    """Transparent source attribution for an article (spec section 19).

    Sources are evidence, not the article itself.
    """

    __tablename__ = "article_sources"
    __table_args__ = (UniqueConstraint("article_id", "source_id", name="uq_article_source"),)

    article_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("articles.id"), index=True, nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sources.id"), nullable=False
    )
    source_report_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("source_reports.id")
    )
    # Denormalized link so the rendered "Sources" section survives source edits.
    url: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False)
    is_independent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ArticleImage(Base, UUIDMixin, TimestampMixin):
    """Metadata for a generated image — NEVER the image bytes (spec section 20).

    We deliberately do not store PNG/JPG/WEBP/video on our infrastructure.
    """

    __tablename__ = "article_images"

    article_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("articles.id"), index=True, nullable=False
    )
    image_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_id: Mapped[str | None] = mapped_column(String(200))
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Temporary provider URL, displayed only under the provider's terms.
    ephemeral_url: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
