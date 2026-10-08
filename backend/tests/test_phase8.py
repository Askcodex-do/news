"""Phase 8 unit tests: hardening primitives (spec section 34/36).

Covers the pieces that do not need a database: outbound URL validation, the
rate-limit stores, the AI cost guard, the production config guard, and
security-event logging. Behaviour under real concurrency and a real database is
in ``test_phase8_db.py``.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.api.middleware import InProcessStore, RateLimitMiddleware, RedisStore
from app.core.config import settings
from app.core.security_log import log_rate_limited
from app.services import cost_control
from app.services.url_safety import UnsafeURL, validate_outbound_url

# --- Outbound URL validation (SSRF guard) ------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://93.184.216.34/feed.xml",
        "file:///etc/passwd",
        "gopher://93.184.216.34/",
    ],
)
def test_non_http_schemes_are_refused(url):
    with pytest.raises(UnsafeURL):
        validate_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/feed.xml",
        "http://10.1.2.3/feed.xml",
        "http://192.168.1.1/feed.xml",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata endpoint
        "http://[::1]/feed.xml",
        "http://0.0.0.0/feed.xml",
    ],
)
def test_private_and_link_local_hosts_are_refused(url):
    with pytest.raises(UnsafeURL):
        validate_outbound_url(url)


def test_public_ip_host_is_allowed():
    url = "http://93.184.216.34/feed.xml"
    assert validate_outbound_url(url) == url


def test_url_without_host_is_refused():
    with pytest.raises(UnsafeURL):
        validate_outbound_url("http:///feed.xml")


def test_private_hosts_allowed_when_guard_disabled(monkeypatch):
    monkeypatch.setattr(settings, "block_private_fetch_hosts", False, raising=False)
    url = "http://127.0.0.1/feed.xml"
    assert validate_outbound_url(url) == url


# --- Rate-limit stores --------------------------------------------------------


async def test_in_process_store_enforces_limit_and_reports_retry_after():
    store = InProcessStore()
    for _ in range(3):
        allowed, _ = await store.hit("client", limit=3, window_seconds=60)
        assert allowed
    allowed, retry_after = await store.hit("client", limit=3, window_seconds=60)
    assert allowed is False
    assert retry_after >= 1


async def test_in_process_store_is_per_key():
    store = InProcessStore()
    assert (await store.hit("a", limit=1, window_seconds=60))[0] is True
    assert (await store.hit("b", limit=1, window_seconds=60))[0] is True
    assert (await store.hit("a", limit=1, window_seconds=60))[0] is False


class _BrokenRedis:
    async def incr(self, *_args, **_kwargs):
        raise ConnectionError("redis is down")

    async def expire(self, *_args, **_kwargs):  # pragma: no cover - never reached
        raise AssertionError("expire must not be called when incr fails")

    async def aclose(self):  # pragma: no cover
        return None


async def test_redis_store_fails_open_when_redis_is_unavailable():
    """A Redis outage must not block traffic (availability first)."""
    store = RedisStore(_BrokenRedis())
    allowed, _ = await store.hit("client", limit=1, window_seconds=60)
    assert allowed is True


# --- Rate-limit middleware end to end ----------------------------------------


def _rate_limited_client(store: InProcessStore, *, limit: int) -> httpx.AsyncClient:
    async def ok(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/x", ok), Route("/health", ok)])
    app.add_middleware(RateLimitMiddleware, store=store, requests_per_minute=limit)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_middleware_returns_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True, raising=False)
    # _limit_for reads the settings value, so pin it for a deterministic test.
    monkeypatch.setattr(settings, "rate_limit_requests_per_minute", 2, raising=False)
    async with _rate_limited_client(InProcessStore(), limit=2) as client:
        assert (await client.get("/x")).status_code == 200
        assert (await client.get("/x")).status_code == 200
        resp = await client.get("/x")
        assert resp.status_code == 429
        assert resp.headers["Retry-After"].isdigit()


async def test_middleware_exempts_health(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True, raising=False)
    monkeypatch.setattr(settings, "rate_limit_requests_per_minute", 1, raising=False)
    async with _rate_limited_client(InProcessStore(), limit=1) as client:
        for _ in range(5):
            assert (await client.get("/health")).status_code == 200


# --- AI cost guard ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_budget_counter():
    cost_control.set_counter(None)
    yield
    cost_control.set_counter(None)


async def test_budget_allows_until_limit_then_defers(monkeypatch):
    monkeypatch.setattr(settings, "ai_budget_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ai_max_requests_per_hour", 2, raising=False)
    assert (await cost_control.reserve_ai_call()).allowed is True
    assert (await cost_control.reserve_ai_call()).allowed is True
    third = await cost_control.reserve_ai_call()
    assert third.allowed is False
    assert third.used == 3 and third.limit == 2


async def test_budget_disabled_never_defers(monkeypatch):
    monkeypatch.setattr(settings, "ai_budget_enabled", False, raising=False)
    monkeypatch.setattr(settings, "ai_max_requests_per_hour", 1, raising=False)
    for _ in range(5):
        assert (await cost_control.reserve_ai_call()).allowed is True


async def test_budget_snapshot_reports_usage(monkeypatch):
    monkeypatch.setattr(settings, "ai_budget_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ai_max_requests_per_hour", 5, raising=False)
    await cost_control.reserve_ai_call()
    snap = await cost_control.budget_snapshot()
    assert snap.used == 1
    assert snap.limit == 5
    assert snap.remaining == 4
    assert snap.allowed is True


async def test_budget_fails_open_when_counter_errors(monkeypatch):
    class _Boom:
        async def incr(self, *_a, **_k):
            raise RuntimeError("counter down")

        async def current(self, *_a, **_k):  # pragma: no cover
            raise RuntimeError("counter down")

    monkeypatch.setattr(settings, "ai_budget_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ai_max_requests_per_hour", 1, raising=False)
    cost_control.set_counter(_Boom())
    assert (await cost_control.reserve_ai_call()).allowed is True


# --- Production config guard --------------------------------------------------


def test_production_problems_ignored_outside_production(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development", raising=False)
    assert settings.production_problems() == []


def test_production_problems_flags_weak_defaults(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production", raising=False)
    monkeypatch.setattr(settings, "admin_api_token", "change-me", raising=False)
    monkeypatch.setattr(
        settings, "database_url", "postgresql+asyncpg://x:change-me@db/news", raising=False
    )
    monkeypatch.setattr(settings, "trust_proxy_headers", False, raising=False)
    problems = settings.production_problems()
    assert len(problems) == 3


def test_production_problems_clean_config(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production", raising=False)
    monkeypatch.setattr(settings, "admin_api_token", "s3cret-real-token", raising=False)
    monkeypatch.setattr(
        settings, "database_url", "postgresql+asyncpg://x:strong@db/news", raising=False
    )
    monkeypatch.setattr(settings, "trust_proxy_headers", True, raising=False)
    assert settings.production_problems() == []


# --- Security logging ---------------------------------------------------------


def test_rate_limit_log_does_not_leak_raw_ip(caplog):
    with caplog.at_level(logging.WARNING, logger="security"):
        log_rate_limited(path="/feed", key="203.0.113.7", limit=120)
    assert "203.0.113.7" not in caplog.text
    assert "rate limit exceeded" in caplog.text
