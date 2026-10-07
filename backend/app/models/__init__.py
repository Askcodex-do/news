"""Import every model so that Alembic autogenerate and `Base.metadata` see them."""

from __future__ import annotations

from app.db.base import Base
from app.models.article import Article, ArticleImage, ArticleSource, ArticleVersion
from app.models.country import Country
from app.models.event import (
    Event,
    EventConflict,
    EventFact,
    EventReport,
    EventUpdate,
)
from app.models.job import ProcessingJob
from app.models.source import (
    CountrySource,
    Source,
    SourceHealth,
    SourceReliability,
)
from app.models.source_report import SourceReport

__all__ = [
    "Base",
    "Article",
    "ArticleImage",
    "ArticleSource",
    "ArticleVersion",
    "Country",
    "CountrySource",
    "Event",
    "EventConflict",
    "EventFact",
    "EventReport",
    "EventUpdate",
    "ProcessingJob",
    "Source",
    "SourceHealth",
    "SourceReliability",
    "SourceReport",
]
