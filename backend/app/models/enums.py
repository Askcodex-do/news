"""Enumerations shared across models, schemas and services.

These are Python/DB enums, not free strings, so that the publication-state
machine described in the spec (section 15) cannot drift.
"""

from __future__ import annotations

from enum import StrEnum


class SourceType(StrEnum):
    rss = "rss"
    atom = "atom"
    api = "api"


class SourceReportStatus(StrEnum):
    NEW = "NEW"
    PROCESSED = "PROCESSED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class EventStatus(StrEnum):
    """Publication state machine (spec section 15)."""

    DETECTED = "DETECTED"
    CLUSTERING = "CLUSTERING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    EDITORIAL_REVIEW = "EDITORIAL_REVIEW"
    PUBLISHED = "PUBLISHED"
    UPDATING = "UPDATING"
    ARCHIVED = "ARCHIVED"
    UNVERIFIED = "UNVERIFIED"
    WAIT = "WAIT"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD = "DEAD"


class ConfidenceBand(StrEnum):
    """Configurable confidence bands (spec section 12)."""

    HIGHLY_CONFIRMED = "HIGHLY_CONFIRMED"  # 95-100
    STRONGLY_SUPPORTED = "STRONGLY_SUPPORTED"  # 85-94
    REASONABLY_SUPPORTED = "REASONABLY_SUPPORTED"  # 70-84
    UNCERTAIN = "UNCERTAIN"  # 50-69
    DO_NOT_PUBLISH = "DO_NOT_PUBLISH"  # <50
