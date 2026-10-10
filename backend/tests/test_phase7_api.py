"""Integration tests for the Phase 7 ops/accuracy endpoints (spec section 33).

These hit the real API and database.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.db.session import SessionLocal
from app.main import app
from app.services.seeding import seed
from tests.conftest import database_available

ADMIN_HEADERS = {"X-Admin-Token": settings.admin_api_token}


@pytest.fixture(scope="session")
async def client():
    if not await database_available():
        pytest.skip("test database not available (see conftest for setup)")

    async with SessionLocal() as session:
        await seed(session)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_metrics_requires_admin(client):
    assert (await client.get("/admin/metrics")).status_code == 401


async def test_metrics_snapshot_shape(client):
    resp = await client.get("/admin/metrics", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert {"generated_at", "sources", "ingestion", "events", "jobs"} <= body.keys()
    assert {"total", "enabled", "healthy", "down", "offline"} <= body["sources"].keys()
    assert "reports_last_hour" in body["ingestion"]
    assert "by_status" in body["events"]
    assert "queue" in body["jobs"]


async def test_accuracy_requires_admin(client):
    assert (await client.get("/admin/accuracy")).status_code == 401


async def test_accuracy_dashboard_shape(client):
    resp = await client.get("/admin/accuracy", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    # The spec calls out these exact signals (section 33).
    for key in (
        "articles_published",
        "fact_validation_failures",
        "corrections",
        "source_conflicts_open",
        "low_confidence_publications",
        "duplicate_publications",
    ):
        assert key in body
    assert body["confidence_floor"] > 0


async def test_maintenance_run_requires_admin(client):
    assert (await client.post("/admin/maintenance/run")).status_code == 401


async def test_maintenance_run_returns_sweep_counts(client):
    resp = await client.post("/admin/maintenance/run", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"events_archived", "jobs_pruned"}
    assert isinstance(body["events_archived"], int)
    assert isinstance(body["jobs_pruned"], int)
