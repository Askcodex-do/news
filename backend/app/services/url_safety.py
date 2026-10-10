"""Outbound URL validation (spec section 34: validate all external URLs).

Feed URLs come from configuration, not from visitors, but they are still an
untrusted input surface: a typo or a compromised config should not turn the
ingestion worker into an SSRF primitive that probes the internal network. This
module refuses any URL whose host resolves to a loopback, private, link-local,
reserved or otherwise non-global address.

Resolution happens immediately before the fetch. This is defence in depth, not
a complete TOCTOU-proof guard (a hostile DNS server could still re-resolve
between the check and the connection); it closes the realistic misconfiguration
and casual-attack cases, which is the spec's bar here.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security_log import log_url_rejected

logger = get_logger(__name__)

_ALLOWED_SCHEMES = ("http", "https")


class UnsafeURL(ValueError):
    """Raised when a URL must not be fetched."""


def _is_global_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return False
    if ip.is_multicast or ip.is_unspecified:
        return False
    # `is_global` catches ranges the flags above miss (e.g. shared address
    # space 100.64/10, benchmarking ranges).
    return ip.is_global


def _resolves_to_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        # An unresolvable host is a fetch failure, not an SSRF risk; let the
        # fetch surface the error and be retried with backoff.
        raise UnsafeURL(f"could not resolve host {host!r}: {exc}") from exc
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not _is_global_ip(ip):
            return True
    return False


def validate_outbound_url(url: str) -> str:
    """Validate a URL for outbound fetch and return it unchanged.

    Raises ``UnsafeURL`` when the URL is not http(s) or its host is not a
    public address. The check can be disabled with
    ``BLOCK_PRIVATE_FETCH_HOSTS=false`` for a deployment that intentionally
    reads from an internal feed host.
    """
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        log_url_rejected(url=url, reason=f"scheme {parts.scheme!r}")
        raise UnsafeURL(f"refusing to fetch non-http(s) URL: {url}")
    host = parts.hostname
    if not host:
        log_url_rejected(url=url, reason="missing host")
        raise UnsafeURL(f"URL has no host: {url}")

    if settings.block_private_fetch_hosts and _resolves_to_private(host):
        log_url_rejected(url=url, reason="host resolves to a non-public address")
        raise UnsafeURL(f"refusing to fetch non-public host: {host}")

    return url
