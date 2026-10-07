"""Shared pytest fixtures and test database selection.

Tests never touch the database the running stack uses. Before any application
module is imported we point `DATABASE_URL` at a dedicated test database
(`news_test` by default), because the engine in `app.db.session` is created at
import time from that setting.

Override with `TEST_DATABASE_URL` to use a different database. The test database
must exist and be migrated (`alembic upgrade head`); database-backed tests skip
automatically when it is missing or unmigrated.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _test_database_url() -> str:
    """Resolve the test database URL, reusing configured credentials."""
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit

    base = os.environ.get("DATABASE_URL")
    if not base:
        env_file = REPO_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("DATABASE_URL="):
                    base = line.split("=", 1)[1].strip()
                    break
    if not base:
        base = "postgresql+asyncpg://news_app:change-me@localhost:5432/news"
    # Same host/credentials, but never the live database.
    return base.rsplit("/", 1)[0] + "/news_test"


os.environ["DATABASE_URL"] = _test_database_url()

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal, engine  # noqa: E402

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example News</title>
    <link>https://example.com</link>
    <language>en</language>
    <item>
      <title>Earthquake strikes Japan killing 12</title>
      <link>https://example.com/story?id=123&amp;utm_source=x</link>
      <description>&lt;p&gt;A magnitude 6.8 earthquake hit Japan.&lt;/p&gt;</description>
      <pubDate>Mon, 06 Oct 2025 09:00:00 GMT</pubDate>
      <author>newsroom@example.com</author>
    </item>
    <item>
      <title>Twelve killed after earthquake hits Japan</title>
      <link>https://example.com/other</link>
      <description>A second report.</description>
      <pubDate>Mon, 06 Oct 2025 09:10:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


async def database_available() -> bool:
    """True when the test database is reachable AND migrated."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM sources LIMIT 1"))
        return True
    except Exception:
        return False


@pytest.fixture
def sample_rss_bytes() -> bytes:
    return SAMPLE_RSS.encode("utf-8")


@pytest.fixture
async def db_session():
    """A session against the test database; skips when it is unavailable."""
    if not await database_available():
        pytest.skip("test database not available (see conftest for setup)")
    async with SessionLocal() as session:
        yield session
        await session.rollback()
