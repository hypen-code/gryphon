"""Security policies — read-only enforcement and exact-host egress validation."""

from __future__ import annotations

import ipaddress

import httpx

from gryphon.errors import SecurityViolationError
from gryphon.utils.logging import get_logger

logger = get_logger(__name__)

# HTTP methods that mutate state
_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})
_METADATA_HOSTS: frozenset[str] = frozenset({"metadata.google.internal", "metadata", "instance-data"})
_METADATA_ADDRESSES = frozenset({"168.63.129.16", "100.100.100.200", "fd00:ec2::254"})
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def enforce_read_only(method: str, server_name: str) -> None:
    """Reject all methods other than the explicitly recognized read methods.

    Args:
        method: HTTP method to check.
        server_name: Server identifier, never included in error details.
    """
    if method.upper() not in _READ_METHODS:
        logger.warning("read_only_violation", violation_type="write_method")
        raise SecurityViolationError("Server is read-only; operation is not permitted")


def validated_url(url: str) -> httpx.URL:
    """Parse a canonical HTTP URL without credentials, fragments or ambiguous syntax.

    Args:
        url: Trusted configuration URL to validate before use.

    Returns:
        Parsed URL with a normalized hostname.
    """
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url or "#" in url:
        raise SecurityViolationError("Invalid upstream URL")
    authority = url.partition("://")[2].split("/", 1)[0].split("?", 1)[0]
    if "@" in authority:
        raise SecurityViolationError("Invalid upstream URL")
    try:
        parsed = httpx.URL(url)
        if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.userinfo:
            raise SecurityViolationError("Invalid upstream URL")
        if "%" in parsed.host or parsed.port == 0:
            raise SecurityViolationError("Invalid upstream URL")
        return parsed.copy_with(host=parsed.host.lower().rstrip("."))
    except (httpx.InvalidURL, ValueError):
        raise SecurityViolationError("Invalid upstream URL") from None


def check_domain_allowed(url: str, allowed_domains: list[str]) -> None:
    """Apply an exact hostname allowlist in addition to network-address validation.

    Args:
        url: HTTP URL without userinfo or fragments.
        allowed_domains: Explicit hostnames; an empty list adds no restriction.
    """
    hostname = validated_url(url).host
    if not allowed_domains:
        return  # No additional hostname restrictions configured
    allowed = {httpx.URL("https://" + domain).host.lower().rstrip(".") for domain in allowed_domains}
    if hostname not in allowed:
        logger.warning("domain_blocked", violation_type="hostname_policy")
        raise SecurityViolationError("Upstream host is not in the allowed domains list")


def check_address_allowed(
    address: str,
    allow_private_networks: bool = False,
    *,
    require_private: bool = False,
) -> None:
    """Reject special-purpose addresses and require private opt-in for plaintext HTTP.

    Args:
        address: Numeric address returned by the trusted resolver.
        allow_private_networks: Administrator permission for private/loopback services.
        require_private: Restrict plaintext HTTP to explicitly permitted private/loopback IPs.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise SecurityViolationError("Invalid upstream address") from None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    always_blocked = (
        ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or (ip.is_reserved and not ip.is_loopback)
        or str(ip) in _METADATA_ADDRESSES
    )
    if isinstance(ip, ipaddress.IPv6Address) and (ip.sixtofour is not None or ip.teredo is not None):
        always_blocked = True
    private_allowed = allow_private_networks and (ip.is_loopback or any(ip in net for net in _PRIVATE_NETWORKS))
    if always_blocked or not (ip.is_global or private_allowed):
        raise SecurityViolationError("Upstream address is prohibited by network policy")
    if require_private and not private_allowed:
        raise SecurityViolationError("Public upstreams require HTTPS; private HTTP requires explicit opt-in")


def check_metadata_host(hostname: str) -> None:
    """Reject well-known metadata aliases regardless of private-network opt-in.

    Args:
        hostname: Canonical hostname.
    """
    if hostname in _METADATA_HOSTS or hostname.endswith(".metadata.google.internal"):
        raise SecurityViolationError("Upstream host is prohibited by network policy")
