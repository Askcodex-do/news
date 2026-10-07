"""Integration tests for the Phase 3 admin/ops endpoints (spec section 33).

These hit the real API and database, exercising the accuracy dashboard and the
event-evidence endpoints rather than mocks.
"""

from __future__ import annotations

import httpx
import pytest

from app.db.session import SessionLocal
from app.main import app
from app.services.seeding import seed
from tests.conftest import database_available

ADMIN_HEADERS = {"X-Admin-Token": "dev-admin-token"}


@pytest.fixture(scope="session")
async def client():
    if not await database_available():
        pytest.skip("test database not available (see conftest for setup)")

    async with SessionLocal() as session:
        await seed(session)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_intelligence_stats_requires_admin(client):
    assert (await client.get("/admin/intelligence/stats")).status_code == 401
    resp = await client.get("/admin/intelligence/stats", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert "events_total" in body
    assert "events_by_status" in body
    assert "conflicts_unresolved" in body
    assert "low_confidence_published" in body


async def test_admin_events_endpoint_returns_evidence(client):
    resp = await client.get("/admin/events", headers=ADMIN_HEADERS, params={"limit": 5})
    assert resp.status_code == 200
    events = resp.json()
    assert isinstance(events, list)
    for event in events:
        assert "facts" in event
        assert "conflicts" in event
        assert "independent_source_count" in event


async def test_admin_events_status_filter_is_accepted(client):
    resp = await client.get("/admin/events", headers=ADMIN_HEADERS, params={"status": "VERIFIED"})
    assert resp.status_code == 200
    for event in resp.json():
        assert event["status"] == "VERIFIED"


async def test_admin_jobs_can_filter_by_cluster_type(client):
    resp = await client.get(
        "/admin/jobs", headers=ADMIN_HEADERS, params={"job_type": "cluster_event"}
    )
    assert resp.status_code == 200
    for job in resp.json():
        assert job["job_type"] == "cluster_event"


async def test_verify_unknown_event_returns_404(client):
    import uuid

    resp = await client.post(f"/admin/events/{uuid.uuid4()}/verify", headers=ADMIN_HEADERS)
    assert resp.status_code == 404
