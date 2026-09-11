"""Exact-host egress and bounded transport tests; no live DNS or HTTP."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import pytest

from gryphon.errors import ExecutionError, SecurityViolationError
from gryphon.security.network import NetworkClient
from gryphon.security.policies import check_address_allowed, check_domain_allowed, validated_url

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from gryphon.config import GryphonConfig


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/data",
        "ftp://api.example.com/file",
        "https://user:secret@api.example.com",
        "https://api.example.com/#secret",
        "https://api.example.com/#",
        "https://api.example.com/\n",
        "https://api.example.com\\@other.example.com",
        "//api.example.com/path",
        "https://[fe80::1%25eth0]/",
    ],
)
def test_invalid_url_rejected_without_reflection(url: str) -> None:
    with pytest.raises(SecurityViolationError, match="^Invalid upstream URL$"):
        validated_url(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "::1",
        "fd00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "192.0.2.1",
        "100.100.100.200",
        "168.63.129.16",
        "fd00:ec2::254",
        "2002:7f00:1::",
        "2001::1",
    ],
)
def test_nonpublic_address_blocked_by_default(address: str) -> None:
    with pytest.raises(SecurityViolationError, match="network policy"):
        check_address_allowed(address)


@pytest.mark.parametrize("address", ["10.0.0.1", "192.168.1.1", "127.0.0.1", "::1", "fd00::1"])
def test_private_admin_opt_in(address: str) -> None:
    check_address_allowed(address, allow_private_networks=True)


@pytest.mark.parametrize("address", ["169.254.169.254", "168.63.129.16", "fd00:ec2::254", "0.0.0.0", "224.0.0.1"])
def test_metadata_never_allowed_by_private_opt_in(address: str) -> None:
    with pytest.raises(SecurityViolationError):
        check_address_allowed(address, allow_private_networks=True)


def test_hostname_case_and_terminal_dot_are_canonical() -> None:
    check_domain_allowed("https://API.EXAMPLE.COM./path", ["api.example.com"])


def test_allowlist_does_not_imply_subdomains() -> None:
    with pytest.raises(SecurityViolationError):
        check_domain_allowed("https://nested.api.example.com", ["api.example.com"])


async def test_mixed_public_private_dns_fails_closed(gryphon_config: GryphonConfig) -> None:
    async def mixed(host: str, port: int) -> list[str]:
        """Emulate a response with both public and forbidden addresses."""
        return ["93.184.216.34", "127.0.0.1"]

    client = NetworkClient(gryphon_config, resolver=mixed)
    try:
        with pytest.raises(SecurityViolationError, match="network policy"):
            await client.request("GET", "https://api.example.com")
    finally:
        await client.close()


async def test_rebinding_does_not_reach_transport(gryphon_config: GryphonConfig) -> None:
    resolutions = 0
    requests: list[httpx.Request] = []

    async def rebinding(host: str, port: int) -> list[str]:
        """Switch DNS answers after the first successful request."""
        nonlocal resolutions
        resolutions += 1
        return ["93.184.216.34"] if resolutions == 1 else ["127.0.0.1"]

    def handle(request: httpx.Request) -> httpx.Response:
        """Record only requests that reached the numeric transport."""
        requests.append(request)
        return httpx.Response(200, json={})

    client = NetworkClient(gryphon_config, resolver=rebinding, transport=httpx.MockTransport(handle))
    try:
        await client.request("GET", "https://api.example.com")
        with pytest.raises(SecurityViolationError):
            await client.request("GET", "https://api.example.com")
        assert len(requests) == 1
    finally:
        await client.close()


async def _public(host: str, port: int) -> list[str]:
    """Resolve a mock public address."""
    return ["93.184.216.34"]


async def test_redirect_is_not_followed(gryphon_config: GryphonConfig) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """Offer a forbidden redirect that must never be requested."""
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})

    client = NetworkClient(gryphon_config, resolver=_public, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ExecutionError, match="^Upstream returned HTTP 302$"):
            await client.request("GET", "https://api.example.com")
        assert len(requests) == 1
    finally:
        await client.close()


class ChunkStream(httpx.AsyncByteStream):
    """A streaming body whose size is not announced in a header."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield bounded chunks that exceed the configured total size."""
        for _ in range(4):
            yield b"x" * 512


async def test_streaming_size_limit_without_content_length(gryphon_config: GryphonConfig) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        """Return a streaming response with no length metadata."""
        return httpx.Response(200, stream=ChunkStream())

    client = NetworkClient(gryphon_config, resolver=_public, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ExecutionError, match="size limit"):
            await client.request("GET", "https://api.example.com", max_bytes=1024)
    finally:
        await client.close()


async def test_compressed_response_is_rejected_before_decode(gryphon_config: GryphonConfig) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        """Return an unsupported content encoding without eager decompression."""
        return httpx.Response(200, stream=ChunkStream(), headers={"Content-Encoding": "gzip"})

    client = NetworkClient(gryphon_config, resolver=_public, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ExecutionError, match="compressed"):
            await client.request("GET", "https://api.example.com")
    finally:
        await client.close()


async def test_cookies_are_not_implicitly_reused(gryphon_config: GryphonConfig) -> None:
    cookies: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """Offer ambient cookies and capture any accidental replay."""
        cookies.append(request.headers.get("Cookie", ""))
        return httpx.Response(200, json={}, headers={"Set-Cookie": "session=untrusted; Path=/"})

    client = NetworkClient(gryphon_config, resolver=_public, transport=httpx.MockTransport(handle))
    try:
        await client.request("GET", "https://api.example.com/first")
        await client.request("GET", "https://api.example.com/second")
        assert cookies == ["", ""]
    finally:
        await client.close()


async def test_error_does_not_reflect_url_or_headers(gryphon_config: GryphonConfig) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        """Raise an HTTPX error containing deliberately sensitive diagnostic data."""
        raise httpx.ConnectError("https://sensitive.example.com token=private", request=request)

    client = NetworkClient(gryphon_config, resolver=_public, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ExecutionError, match="^Upstream request failed$"):
            await client.request("GET", "https://api.example.com")
    finally:
        await client.close()


async def test_total_timeout_includes_dns(gryphon_config: GryphonConfig) -> None:
    async def blocked(host: str, port: int) -> list[str]:
        """Wait forever without issuing a DNS request."""
        await asyncio.Event().wait()
        return []

    client = NetworkClient(gryphon_config, resolver=blocked)
    try:
        with pytest.raises(ExecutionError, match="^Upstream request failed$"):
            await client.request("GET", "https://api.example.com", timeout=0.01)
    finally:
        await client.close()
