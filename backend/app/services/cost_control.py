"""AI spend guard (spec sections 33-34: API cost controls).

Every AI call passes through :func:`reserve_ai_call`. The hourly budget
(``AI_MAX_REQUESTS_PER_HOUR``) is enforced by a shared counter so a scaled
deployment cannot exceed it: with Redis configured the counter is atomic across
workers, otherwise a per-process counter is used.

When the budget is exhausted the caller must **defer**, not fail: generation is
skipped for this pass and the event stays in its pre-publication state, so it
will be picked up again once the window resets. Accuracy is never traded for
volume (spec section 35), and neither is availability traded for cost.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_BUCKET_SECONDS = 3600


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    used: int
    limit: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


class _Counter:
    """Interface for the two counter backends."""

    async def incr(self, key: str, ttl_seconds: int) -> int:  # pragma: no cover
        raise NotImplementedError

    async def current(self, key: str) -> int:  # pragma: no cover
        raise NotImplementedError


class _MemoryCounter(_Counter):
    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, float]] = {}

    async def incr(self, key: str, ttl_seconds: int) -> int:
        now = time.time()
        count, expires = self._counts.get(key, (0, now + ttl_seconds))
        if now >= expires:
            count, expires = 0, now + ttl_seconds
        count += 1
        self._counts[key] = (count, expires)
        return count

    async def current(self, key: str) -> int:
        count, expires = self._counts.get(key, (0, 0.0))
        if time.time() >= expires:
            return 0
        return count


class _RedisCounter(_Counter):
    def __init__(self, client) -> None:  # type: ignore[no-untyped-def]
        self._client = client

    async def incr(self, key: str, ttl_seconds: int) -> int:
        count = await self._client.incr(key)
        if count == 1:
            await self._client.expire(key, ttl_seconds)
        return int(count)

    async def current(self, key: str) -> int:
        value = await self._client.get(key)
        return int(value) if value else 0


def _hour_key() -> str:
    return f"ai_budget:{int(time.time()) // _BUCKET_SECONDS}"


_counter: _Counter | None = None


def set_counter(counter: _Counter | None) -> None:
    """Override the counter backend (tests, or a deployment-injected store)."""
    global _counter
    _counter = counter


def get_counter() -> _Counter:
    global _counter
    if _counter is None:
        _counter = _build_counter()
    return _counter


def _build_counter() -> _Counter:
    if settings.rate_limit_redis_enabled:
        try:
            import redis.asyncio as redis_asyncio

            return _RedisCounter(
                redis_asyncio.from_url(settings.redis_url, socket_connect_timeout=1)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not build Redis budget counter, using in-memory: %s", exc)
    return _MemoryCounter()


async def reserve_ai_call(*, units: int = 1) -> BudgetDecision:
    """Reserve `units` AI calls against the hourly budget.

    Returns ``allowed=False`` when the budget is already spent. Never raises on
    a counter outage: a metrics-store failure must not stop accurate publishing,
    so an unavailable counter allows the call and logs loudly.
    """
    limit = settings.ai_max_requests_per_hour
    if not settings.ai_budget_enabled or limit <= 0:
        return BudgetDecision(allowed=True, used=0, limit=0)

    try:
        used = await get_counter().incr(_hour_key(), _BUCKET_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI budget counter unavailable, allowing call: %s", exc)
        return BudgetDecision(allowed=True, used=0, limit=limit)

    if used > limit:
        logger.warning("AI hourly budget exhausted (%d/%d); deferring generation", used, limit)
        return BudgetDecision(allowed=False, used=used, limit=limit)
    return BudgetDecision(allowed=True, used=used, limit=limit)


async def budget_snapshot() -> BudgetDecision:
    """Current usage for observability (spec section 33)."""
    limit = settings.ai_max_requests_per_hour
    if not settings.ai_budget_enabled or limit <= 0:
        return BudgetDecision(allowed=True, used=0, limit=0)
    try:
        used = await get_counter().current(_hour_key())
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI budget counter unavailable: %s", exc)
        used = 0
    return BudgetDecision(allowed=used < limit, used=used, limit=limit)
