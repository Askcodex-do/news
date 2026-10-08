"""Rate limiting middleware (spec section 34).

Two stores implement the same small interface:

* ``InProcessStore`` - a per-process sliding window. Correct for a single
  instance and for tests.
* ``RedisStore`` - a fixed-window counter shared by every replica, so the limit
  is enforced across a scaled deployment. Selected automatically when
  ``RATE_LIMIT_REDIS_ENABLED`` is set and Redis is reachable; if Redis is
  unavailable the middleware falls back to the in-process store rather than
  failing requests (accuracy and availability first, section 35).

The visitor's IP is used only as a rate-limit key and is never stored: keys are
hashed before they reach the logs (see ``app.core.security_log``).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Protocol

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security_log import log_rate_limited

logger = get_logger(__name__)


class RateLimitStore(Protocol):
    async def hit(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Record a hit; return ``(allowed, retry_after_seconds)``."""

    async def close(self) -> None:
        """Release any resources (no-op for the in-process store)."""


class InProcessStore:
    """Per-process sliding window. Not shared across replicas."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        window = self._hits[key]
        while window and now - window[0] > window_seconds:
            window.popleft()
        if len(window) >= limit:
            retry_after = max(1, int(window_seconds - (now - window[0])))
            return False, retry_after
        window.append(now)
        return True, 0

    async def close(self) -> None:  # pragma: no cover - nothing to release
        return None


class RedisStore:
    """Fixed-window counter shared by all replicas (atomic INCR + EXPIRE)."""

    def __init__(self, client) -> None:  # type: ignore[no-untyped-def]
        self._client = client

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        bucket = f"ratelimit:{key}:{int(time.time()) // window_seconds}"
        try:
            count = await self._client.incr(bucket)
            if count == 1:
                await self._client.expire(bucket, window_seconds)
        except Exception as exc:  # noqa: BLE001 - a Redis outage must not block traffic
            logger.warning("rate-limit store unavailable, allowing request: %s", exc)
            return True, 0
        if count > limit:
            return False, window_seconds
        return True, 0

    async def close(self) -> None:
        await self._client.aclose()


def build_rate_limit_store() -> RateLimitStore:
    """Return the configured store, falling back to in-process."""
    if not settings.rate_limit_redis_enabled:
        return InProcessStore()
    try:
        import redis.asyncio as redis_asyncio

        client = redis_asyncio.from_url(settings.redis_url, socket_connect_timeout=1)
        return RedisStore(client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not build Redis rate-limit store, using in-process: %s", exc)
        return InProcessStore()


def _limit_for(path: str) -> int:
    # The admin surface is stricter than public reads.
    if path.startswith("/admin"):
        return settings.rate_limit_admin_requests_per_minute
    return settings.rate_limit_requests_per_minute


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(  # type: ignore[no-untyped-def]
        self,
        app,
        *,
        store: RateLimitStore | None = None,
        requests_per_minute: int | None = None,
        exempt_paths: set[str] | None = None,
    ):
        super().__init__(app)
        self.store = store or build_rate_limit_store()
        self._default_limit = requests_per_minute or settings.rate_limit_requests_per_minute
        self.exempt_paths = exempt_paths if exempt_paths is not None else {"/health"}
        self.enabled = settings.rate_limit_enabled

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        if not self.enabled or request.url.path in self.exempt_paths:
            return await call_next(request)

        client = request.client.host if request.client else "unknown"
        limit = _limit_for(request.url.path)
        allowed, retry_after = await self.store.hit(
            client, limit=limit, window_seconds=settings.rate_limit_window_seconds
        )
        if not allowed:
            log_rate_limited(path=request.url.path, key=client, limit=limit)
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)
