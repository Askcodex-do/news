"""Security event logging (spec section 34).

Security-relevant events are emitted on a dedicated logger so an operator can
route or alert on them independently of ordinary application logs. Nothing here
records a visitor IP: rate limiting keys are hashed, and admin auth failures log
the outcome, not the presented token.
"""

from __future__ import annotations

import hashlib
import logging

_logger = logging.getLogger("security")


def _hash(value: str) -> str:
    """Return a short, stable pseudonym for a rate-limit key."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def log_rate_limited(*, path: str, key: str, limit: int) -> None:
    _logger.warning("rate limit exceeded path=%s client=%s limit=%d/min", path, _hash(key), limit)


def log_admin_auth_failure(*, path: str, reason: str) -> None:
    _logger.warning("admin auth failure path=%s reason=%s", path, reason)


def log_url_rejected(*, url: str, reason: str) -> None:
    """An outbound URL was refused (non-http(s), private host, oversize)."""
    _logger.warning("outbound url rejected reason=%s url=%.120s", reason, url)


def log_config_problem(*, detail: str) -> None:
    """A production configuration problem detected at startup."""
    _logger.error("configuration problem: %s", detail)
