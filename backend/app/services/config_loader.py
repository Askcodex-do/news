"""Load and validate the source configuration files.

Application code must obtain source URLs from here (or from the DB seeded from
here), never from hard-coded constants. This keeps the "no hard-coded source
URLs throughout application code" rule (spec section 3) enforceable.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator

from app.core.config import CONFIG_DIR
from app.models.enums import SourceType

INTERNATIONAL_SOURCES_FILE = CONFIG_DIR / "international_sources.yaml"
LOCAL_SOURCES_FILE = CONFIG_DIR / "local_sources.yaml"
COUNTRY_SOURCES_FILE = CONFIG_DIR / "country_sources.yaml"

EXPECTED_INTERNATIONAL_COUNT = 30


class SourceConfig(BaseModel):
    id: str
    name: str
    country: str
    type: SourceType
    rss_url: HttpUrl | None = None
    website_url: HttpUrl
    api_url: HttpUrl | None = None
    enabled: bool = True
    priority: int = Field(ge=0, le=100)
    language: str = "en"
    poll_interval_seconds: int = Field(ge=30, default=300)
    reliability_score: float = Field(ge=0, le=100, default=70.0)
    # Whether the source's terms permit storing the full article body. Default
    # false: we keep title/summary and a link, not the publisher's full text.
    raw_text_permitted: bool = False

    @field_validator("country")
    @classmethod
    def _country_iso2(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise ValueError(f"country must be ISO 3166-1 alpha-2, got {value!r}")
        return value

    @field_validator("rss_url")
    @classmethod
    def _rss_required_for_feeds(cls, value, info):  # type: ignore[no-untyped-def]
        return value

    def model_post_init(self, __context: object) -> None:  # noqa: D105
        if self.type in (SourceType.rss, SourceType.atom) and self.rss_url is None:
            raise ValueError(f"source {self.id!r} is type {self.type} but has no rss_url")


class SourceFile(BaseModel):
    version: int
    sources: list[SourceConfig]


class CountryMapping(BaseModel):
    country_code: str
    primary: str
    backup: str | None = None
    priority: int = Field(ge=0, le=100, default=50)
    enabled: bool = True

    @field_validator("country_code")
    @classmethod
    def _country_iso2(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise ValueError(f"country_code must be ISO 3166-1 alpha-2, got {value!r}")
        return value


class CountrySourceFile(BaseModel):
    version: int
    mappings: list[CountryMapping]


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_international_sources(path: Path | None = None) -> list[SourceConfig]:
    data = _load_yaml(path or INTERNATIONAL_SOURCES_FILE)
    parsed = SourceFile.model_validate(data)
    if len(parsed.sources) != EXPECTED_INTERNATIONAL_COUNT:
        raise ValueError(
            f"expected {EXPECTED_INTERNATIONAL_COUNT} international sources, "
            f"found {len(parsed.sources)}"
        )
    _assert_unique_ids(parsed.sources)
    return parsed.sources


def load_local_sources(path: Path | None = None) -> list[SourceConfig]:
    data = _load_yaml(path or LOCAL_SOURCES_FILE)
    parsed = SourceFile.model_validate(data)
    _assert_unique_ids(parsed.sources)
    return parsed.sources


def load_country_sources(path: Path | None = None) -> list[CountryMapping]:
    data = _load_yaml(path or COUNTRY_SOURCES_FILE)
    return CountrySourceFile.model_validate(data).mappings


def load_all_sources() -> list[SourceConfig]:
    """Every configured source, with `is_international` derived from the file."""
    return load_international_sources() + load_local_sources()


def _assert_unique_ids(sources: list[SourceConfig]) -> None:
    seen: set[str] = set()
    for source in sources:
        if source.id in seen:
            raise ValueError(f"duplicate source id: {source.id!r}")
        seen.add(source.id)
