"""Shared FastAPI dependencies: client IP, admin auth, geolocation."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request, status

from app.core.config import settings
from app.core.security_log import log_admin_auth_failure
from app.services.geoip import GeoIPProvider, NullGeoIPProvider

_geoip_provider: GeoIPProvider = NullGeoIPProvider()


def set_geoip_provider(provider: GeoIPProvider) -> None:
    """Wire the deployment's geolocation backend at startup."""
    global _geoip_provider
    _geoip_provider = provider


def get_geoip_provider() -> GeoIPProvider:
    return _geoip_provider


def get_client_ip(request: Request) -> str | None:
    """Return the visitor IP used only for country resolution.

    X-Forwarded-For is trusted only when the deployment declares it sits behind
    a proxy it controls; otherwise we use the socket peer address.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def require_admin(request: Request, x_admin_token: str | None = Header(default=None)) -> None:
    """Guard admin/ops endpoints with a shared token (constant-time compare)."""
    expected = settings.admin_api_token
    if not expected or expected == "change-me":
        if settings.is_production:
            log_admin_auth_failure(path=request.url.path, reason="token not configured")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="admin API token is not configured",
            )
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        log_admin_auth_failure(path=request.url.path, reason="invalid or missing token")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
