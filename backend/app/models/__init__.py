"""Import every model so that Alembic autogenerate and `Base.metadata` see them."""

from __future__ import annotations

from app.db.base import Base
from app.models.article import Article, ArticleImage, ArticleSource, ArticleVersion
from app.models.country import Country
from app.models.event import (
    Event,
    EventConflict,
    EventFact,
    EventFactSource,
    EventReport,
    EventUpdate,
)
from app.models.job import ProcessingJob
from app.models.scheduler_state import SchedulerState
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
    "EventFactSource",
    "EventReport",
    "EventUpdate",
    "ProcessingJob",
    "SchedulerState",
    "Source",
    "SourceHealth",
    "SourceReliability",
    "SourceReport",
]
