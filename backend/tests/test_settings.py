"""Settings normalization used by managed deployments (Render/Heroku)."""

from __future__ import annotations

import pytest

from app.core.config import Settings


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@host:5432/news", "postgresql+asyncpg://u:p@host:5432/news"),
        ("postgres://u:p@host/news", "postgresql+asyncpg://u:p@host/news"),
        (
            "postgresql+asyncpg://u:p@host/news",
            "postgresql+asyncpg://u:p@host/news",
        ),
    ],
)
def test_plain_postgres_url_gets_the_async_driver(given: str, expected: str) -> None:
    """A provider connection string must not select the sync dialect."""
    settings = Settings(database_url=given)
    assert settings.database_url == expected
    assert settings.sync_database_url == expected.replace("+asyncpg", "+psycopg")
