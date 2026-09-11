"""HTTPS-by-default and explicit private HTTP transport policy with no live DNS or HTTP."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest

from gryphon.errors import ExecutionError, SecurityViolationError
from gryphon.models import KeycloakAuthConfig, OAuth2AuthConfig, SessionAuthConfig
from gryphon.security.auth import AsyncVault
from gryphon.security.network import NetworkClient

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig
    from gryphon.models import AuthConfig


@pytest.mark.parametrize("allow_private", [False, True])
@pytest.mark.parametrize(
    "options",
    [
        {},
        {"headers": {"Authorization": "Bearer mock-credential"}},
        {"data": {"username": "mock-user", "password": "mock-password"}},
        {"json_body": {"username": "mock-user", "password": "mock-password"}},
    ],
)
async def test_public_plaintext_never_reaches_transport(
    gryphon_config: GryphonConfig,
    allow_private: bool,
    options: dict[str, Any],
) -> None:
    """Public docs, API calls and login bodies require HTTPS even with private opt-in."""
    gryphon_config.allow_private_networks = allow_private
    requests: list[httpx.Request] = []

    async def resolver(host: str, port: int) -> list[str]:
        """Resolve to a public destination without issuing DNS."""
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        """Record any forbidden transmission to the mock transport."""
        requests.append(request)
        return httpx.Response(200, json={})

    client = NetworkClient(gryphon_config, resolver=resolver, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(
            SecurityViolationError, match="^Public upstreams require HTTPS; private HTTP requires explicit opt-in$"
        ):
            await client.request("POST" if options else "GET", "http://api.example.com", **options)
        assert requests == []
    finally:
        await client.close()


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["::1"],
        ["10.1.2.3"],
        ["172.16.0.1"],
        ["192.168.1.2"],
        ["fd00::1"],
        ["::ffff:127.0.0.1"],
        ["::1", "10.1.2.3"],
    ],
)
async def test_private_plaintext_requires_and_honors_explicit_opt_in(
    gryphon_config: GryphonConfig,
    addresses: list[str],
) -> None:
    """Approved private and loopback answers may use HTTP only after administrator opt-in."""
    requests: list[httpx.Request] = []

    async def resolver(host: str, port: int) -> list[str]:
        """Return only approved local/private addresses."""
        return addresses

    def handler(request: httpx.Request) -> httpx.Response:
        """Capture the pinned HTTP destination for an explicitly permitted local service."""
        requests.append(request)
        return httpx.Response(200, json={})

    client = NetworkClient(gryphon_config, resolver=resolver, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SecurityViolationError):
            await client.request("GET", "http://local.example.com")
        gryphon_config.allow_private_networks = True
        await client.request("GET", "http://local.example.com")
        assert len(requests) == 1 and requests[0].url.scheme == "http" and requests[0].url.host in addresses
    finally:
        await client.close()


@pytest.mark.parametrize(
    "addresses",
    [
        ["10.0.0.1", "93.184.216.34"],
        ["::ffff:93.184.216.34"],
        ["169.254.169.254"],
        ["168.63.129.16"],
        ["100.100.100.200"],
        ["fd00:ec2::254"],
        ["fe80::1"],
        ["0.0.0.0"],
        ["224.0.0.1"],
        ["240.0.0.1"],
        ["192.0.2.1"],
    ],
)
async def test_private_http_opt_in_never_allows_public_mixed_or_special_answers(
    gryphon_config: GryphonConfig,
    addresses: list[str],
) -> None:
    """Every DNS answer must qualify; one public or permanently forbidden IP denies the call."""
    gryphon_config.allow_private_networks = True
    handler = AsyncMock(return_value=httpx.Response(200, json={}))

    async def resolver(host: str, port: int) -> list[str]:
        """Return the adversarial answer set without issuing DNS."""
        return addresses

    client = NetworkClient(gryphon_config, resolver=resolver, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SecurityViolationError):
            await client.request("GET", "http://local.example.com")
        handler.assert_not_called()
    finally:
        await client.close()


@pytest.mark.parametrize("host", ["metadata", "metadata.google.internal", "instance-data"])
async def test_metadata_alias_stays_blocked_before_dns(gryphon_config: GryphonConfig, host: str) -> None:
    """An approved private HTTP profile never grants metadata hostname access."""
    gryphon_config.allow_private_networks = True
    resolver = AsyncMock(return_value=["10.0.0.1"])
    client = NetworkClient(gryphon_config, resolver=resolver)
    try:
        with pytest.raises(SecurityViolationError, match="network policy"):
            await client.request("GET", f"http://{host}")
        resolver.assert_not_called()
    finally:
        await client.close()


async def test_private_http_rebinding_to_public_is_rejected(gryphon_config: GryphonConfig) -> None:
    """A previously approved private connection pool cannot authorize public plaintext rebinding."""
    gryphon_config.allow_private_networks = True
    resolver = AsyncMock(side_effect=[["10.0.0.1"], ["93.184.216.34"]])
    handler = AsyncMock(return_value=httpx.Response(200, json={}))
    client = NetworkClient(gryphon_config, resolver=resolver, transport=httpx.MockTransport(handler))
    try:
        await client.request("GET", "http://local.example.com")
        with pytest.raises(SecurityViolationError, match="require HTTPS"):
            await client.request("GET", "http://local.example.com")
        assert handler.call_count == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "auth",
    [
        OAuth2AuthConfig(token_url="http://auth.example.com/token", client_id="mock", client_secret="mock"),
        KeycloakAuthConfig(base_url="http://auth.example.com", realm="mock", client_id="mock", client_secret="mock"),
        SessionAuthConfig(login_url="http://auth.example.com/login", username="mock", password="mock"),
    ],
)
async def test_dynamic_auth_cannot_transmit_public_plaintext(
    gryphon_config: GryphonConfig,
    auth: AuthConfig,
) -> None:
    """All dynamic authentication types share the public HTTPS-only transport boundary."""
    handler = AsyncMock(return_value=httpx.Response(200, json={}))
    resolver = AsyncMock(return_value=["93.184.216.34"])
    client = NetworkClient(gryphon_config, resolver=resolver, transport=httpx.MockTransport(handler))
    vault = AsyncVault(client)
    try:
        with pytest.raises(SecurityViolationError, match="require HTTPS"):
            await vault.resolve("svc", auth)
        handler.assert_not_called()
    finally:
        vault.close()
        await client.close()


async def test_allowed_private_http_does_not_follow_redirects(gryphon_config: GryphonConfig) -> None:
    """Private HTTP opt-in does not permit any redirect, including one to a public host."""
    gryphon_config.allow_private_networks = True
    resolver = AsyncMock(return_value=["10.0.0.1"])
    handler = AsyncMock(return_value=httpx.Response(302, headers={"Location": "http://public.example.com"}))
    client = NetworkClient(gryphon_config, resolver=resolver, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ExecutionError, match="^Upstream returned HTTP 302$"):
            await client.request("GET", "http://local.example.com")
        assert handler.call_count == 1
    finally:
        await client.close()


@pytest.mark.parametrize("url", ["http://@local.example.com", "http://user:mock@local.example.com"])
async def test_allowed_private_http_still_rejects_userinfo(gryphon_config: GryphonConfig, url: str) -> None:
    """Neither empty nor populated userinfo is permitted by the private HTTP exception."""
    gryphon_config.allow_private_networks = True
    resolver = AsyncMock(return_value=["10.0.0.1"])
    client = NetworkClient(gryphon_config, resolver=resolver)
    try:
        with pytest.raises(SecurityViolationError, match="^Invalid upstream URL$"):
            await client.request("GET", url)
        resolver.assert_not_called()
    finally:
        await client.close()
