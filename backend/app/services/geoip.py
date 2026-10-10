"""Visitor IP -> ISO country code resolution.

Design rules (spec section 5):
  * We only ever need the country code, not the precise IP.
  * We never persist visitor IP addresses.
  * If geolocation fails we return a global/default sentinel; the site is never
    blocked.

This module exposes a provider interface. The default implementation is a
pluggable, DB-free resolver suitable for tests and local development. A real
deployment wires a MaxMind GeoLite2 database or a hosted lookup behind the same
interface.
"""

from __future__ import annotations

import ipaddress
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.core.logging import get_logger

logger = get_logger(__name__)

GLOBAL_COUNTRY = "XX"  # sentinel: "no local edition", falls back to global feed


@dataclass(frozen=True)
class GeoResult:
    country_code: str
    resolved: bool


class GeoIPProvider(ABC):
    @abstractmethod
    def lookup(self, ip: str) -> str | None:
        """Return an ISO country code for `ip`, or None if unknown."""


class NullGeoIPProvider(GeoIPProvider):
    """Always fails to resolve; every visitor gets the global edition."""

    def lookup(self, ip: str) -> str | None:  # noqa: ARG002
        return None


class StaticGeoIPProvider(GeoIPProvider):
    """Deterministic provider for tests: maps CIDR blocks to country codes."""

    def __init__(self, networks: dict[str, str] | None = None) -> None:
        self._networks = [
            (ipaddress.ip_network(cidr), code) for cidr, code in (networks or {}).items()
        ]

    def lookup(self, ip: str) -> str | None:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        for network, code in self._networks:
            if addr in network:
                return code
        return None


class MaxMindGeoIPProvider(GeoIPProvider):
    """GeoLite2 lookup backed by a local .mmdb file.

    The reader is opened lazily and the import is optional, so a deployment that
    has not provisioned the database degrades to the global edition instead of
    failing to start. Nothing about a visitor is logged or persisted here — only
    the ISO country code is ever returned (spec section 5).
    """

    def __init__(self, database_path: str) -> None:
        if not database_path:
            raise ValueError("GEOIP_DATABASE_PATH is required for the maxmind provider")
        import geoip2.database  # imported here so the dependency stays optional

        self._reader = geoip2.database.Reader(database_path)

    def lookup(self, ip: str) -> str | None:
        try:
            response = self._reader.city(ip)
        except Exception:  # noqa: BLE001 - an unknown/private IP is not an error
            return None
        return response.country.iso_code or None


def build_geoip_provider(
    name: str,
    *,
    database_path: str = "",
    static_map: str = "",
) -> GeoIPProvider:
    """Select the geolocation backend from configuration (spec section 5).

    ``null`` resolves nothing (global edition), ``static`` uses an explicit
    CIDR->country JSON map for dev/test, and ``maxmind`` reads a GeoLite2 file.
    Any misconfiguration falls back to ``null`` rather than blocking the site.
    """
    normalized = (name or "null").strip().lower()
    if normalized == "static":
        try:
            networks = json.loads(static_map) if static_map else {}
        except json.JSONDecodeError:
            logger.warning("GEOIP_STATIC_MAP is not valid JSON; using the null provider")
            return NullGeoIPProvider()
        return StaticGeoIPProvider(networks if isinstance(networks, dict) else {})
    if normalized == "maxmind":
        try:
            return MaxMindGeoIPProvider(database_path)
        except Exception as exc:  # noqa: BLE001 - never block startup on GeoIP
            logger.warning("GeoIP maxmind provider unavailable (%s); using null", exc)
            return NullGeoIPProvider()
    return NullGeoIPProvider()


def resolve_country(ip: str | None, provider: GeoIPProvider) -> GeoResult:
    """Resolve a country code for a visitor IP, never raising and never blocking."""
    if not ip:
        return GeoResult(country_code=GLOBAL_COUNTRY, resolved=False)
    try:
        code = provider.lookup(ip)
    except Exception:  # noqa: BLE001 - geolocation must never break a request
        code = None
    if not code:
        return GeoResult(country_code=GLOBAL_COUNTRY, resolved=False)
    return GeoResult(country_code=code.upper(), resolved=True)
