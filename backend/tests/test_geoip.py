from __future__ import annotations

from app.services.geoip import (
    GLOBAL_COUNTRY,
    NullGeoIPProvider,
    StaticGeoIPProvider,
    resolve_country,
)


def test_null_provider_falls_back_to_global():
    result = resolve_country("8.8.8.8", NullGeoIPProvider())
    assert result.country_code == GLOBAL_COUNTRY
    assert result.resolved is False


def test_static_provider_resolves_known_block():
    provider = StaticGeoIPProvider({"203.0.113.0/24": "IN"})
    result = resolve_country("203.0.113.10", provider)
    assert result.country_code == "IN"
    assert result.resolved is True


def test_unknown_ip_falls_back_to_global():
    provider = StaticGeoIPProvider({"203.0.113.0/24": "IN"})
    assert resolve_country("198.51.100.1", provider).country_code == GLOBAL_COUNTRY


def test_missing_ip_is_global_and_does_not_raise():
    assert resolve_country(None, NullGeoIPProvider()).country_code == GLOBAL_COUNTRY


def test_malformed_ip_is_global():
    provider = StaticGeoIPProvider({"203.0.113.0/24": "IN"})
    assert resolve_country("not-an-ip", provider).country_code == GLOBAL_COUNTRY


def test_provider_exception_is_swallowed():
    class Boom(NullGeoIPProvider):
        def lookup(self, ip: str) -> str | None:
            raise RuntimeError("geolocation service down")

    assert resolve_country("8.8.8.8", Boom()).country_code == GLOBAL_COUNTRY
