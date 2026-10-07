"""Integration tests against a real PostgreSQL database.

These exercise the actual SQLAlchemy models, migrations and API handlers rather
than mocks. They require a reachable database (see docker-compose.yml) and are
skipped automatically when one is not available.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text

from app.db.session import SessionLocal, engine
from app.main import app
from app.services.seeding import seed


async def _database_available() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
async def client():
    if not await _database_available():
        pytest.skip("no PostgreSQL database available")

    async with SessionLocal() as session:
        await seed(session)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_reports_database_and_source_count(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["source_count"] >= 30


async def test_sources_endpoint_returns_configured_sources(client):
    resp = await client.get("/sources")
    assert resp.status_code == 200
    slugs = {s["slug"] for s in resp.json()}
    assert "bbc-world" in slugs
    assert "indiatoday" in slugs


async def test_source_detail_and_health_requires_admin(client):
    detail = await client.get("/sources/indiatoday")
    assert detail.status_code == 200
    assert detail.json()["name"] == "India Today"

    assert (await client.get("/sources/indiatoday/health")).status_code == 401
    authorized = await client.get(
        "/sources/indiatoday/health", headers={"X-Admin-Token": "dev-admin-token"}
    )
    assert authorized.status_code == 200
    assert authorized.json()["status"] in {"unknown", "healthy", "degraded", "down"}


async def test_country_sources_mapping_is_queryable(client):
    resp = await client.get("/country-sources", params={"country": "IN"})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["country_code"] == "IN"
    assert rows[0]["source_slug"] == "indiatoday"


async def test_geo_never_blocks_and_defaults_to_global(client):
    resp = await client.get("/geo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["country_code"] == "XX"
    assert body["resolved"] is False


async def test_feed_resolves_local_edition_for_known_country(client):
    resp = await client.get("/feed", params={"country": "IN"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["country_code"] == "IN"
    assert body["local_source_name"] == "India Today"
    assert len(body["global_articles"]) <= 20
    assert len(body["local_articles"]) <= 20


async def test_feed_falls_back_to_global_for_unknown_country(client):
    resp = await client.get("/feed", params={"country": "ZZ"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["country_code"] == "ZZ"
    assert body["local_source_name"] is None
    assert body["local_articles"] == []
