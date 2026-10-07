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
from abc import ABC, abstractmethod
from dataclasses import dataclass

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
