"""Lightweight in-process rate limiter.

Suitable for a single instance / Phase 1. In a multi-instance deployment this
should be replaced by a Redis-backed limiter behind the same middleware hook.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(  # type: ignore[no-untyped-def]
        self,
        app,
        *,
        requests_per_minute: int = 120,
        exempt_paths: set[str] | None = None,
    ):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.exempt_paths = exempt_paths or {"/health"}
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        if request.url.path in self.exempt_paths:
            return await call_next(request)

        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        window = self._hits[client]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.requests_per_minute:
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": "60"},
            )
        window.append(now)
        return await call_next(request)
